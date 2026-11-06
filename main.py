import base64
import hashlib
import json
import logging
import os
import re
import secrets
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel
from psycopg.errors import ForeignKeyViolation, UniqueViolation
from psycopg.types.json import Jsonb
from starlette.exceptions import HTTPException as StarletteHTTPException

load_dotenv()

from db import init_db, pool  # noqa: E402

API_KEY = os.environ.get("API_KEY")
EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
MAX_ATTEMPTS = 5
PUBLIC_DIR = Path(__file__).parent / "public"

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


class NewBooking(BaseModel):
    slot_id: str
    name: str
    email: str


class SlotIn(BaseModel):
    id: str
    provider: str
    start: datetime
    end: datetime


class ResetIn(BaseModel):
    slots: list[SlotIn]


def error(status_code: int, message: str, retry_after: int | None = None):
    headers = None
    if retry_after is not None:
        headers = {"Retry-After": str(retry_after)}
    return JSONResponse(
        status_code=status_code,
        content={"error": message},
        headers=headers,
    )


def ensure_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def to_iso(value: datetime) -> str:
    value = ensure_utc(value).replace(microsecond=0)
    return value.isoformat().replace("+00:00", "Z")


def booking_json(row) -> dict:
    return {
        "id": row["id"],
        "slot_id": row["slot_id"],
        "name": row["name"],
        "email": row["email"],
        "status": row["status"],
        "created_at": to_iso(row["created_at"]),
    }


def slot_json(row) -> dict:
    return {
        "id": row["id"],
        "provider": row["provider"],
        "start": to_iso(row["start_time"]),
        "end": to_iso(row["end_time"]),
        "available": not row["booked"],
    }


def normalize_booking(body: NewBooking) -> dict:
    return {
        "slot_id": body.slot_id.strip(),
        "name": body.name.strip(),
        "email": body.email.strip().lower(),
    }


def request_hash(input_data: dict) -> str:
    payload = json.dumps(input_data, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def encode_cursor(row) -> str:
    payload = json.dumps({"t": row["cursor_time"], "id": row["id"]})
    return base64.urlsafe_b64encode(payload.encode("utf-8")).decode("ascii").rstrip("=")


def decode_cursor(value: str):
    try:
        padded = value + "=" * (-len(value) % 4)
        data = json.loads(base64.urlsafe_b64decode(padded).decode("utf-8"))
        if not isinstance(data, dict):
            return None
        if not isinstance(data.get("t"), str) or not isinstance(data.get("id"), str):
            return None
        datetime.fromisoformat(data["t"].replace("Z", "+00:00"))
        return data
    except Exception:
        return None


def rate_limit(conn, email: str):
    # Serialise attempts for this email so concurrent requests cannot exceed 5.
    conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (f"rate:{email}",))
    row = conn.execute(
        """
        SELECT count(*)::int AS n,
               COALESCE(
                 CEIL(EXTRACT(EPOCH FROM (MIN(attempted_at) + interval '1 minute' - now()))),
                 60
               )::int AS retry_after
        FROM attempts
        WHERE email = %s AND attempted_at > now() - interval '1 minute'
        """,
        (email,),
    ).fetchone()

    if row["n"] >= MAX_ATTEMPTS:
        return max(1, row["retry_after"])

    conn.execute("INSERT INTO attempts (email) VALUES (%s)", (email,))
    return None


def process_booking(conn, input_data: dict) -> dict:
    if not input_data["email"]:
        return {"status": 400, "body": {"error": "email is required"}}

    # Failed attempts count too, so this runs before the remaining validation.
    retry_after = rate_limit(conn, input_data["email"])
    if retry_after is not None:
        return {
            "status": 429,
            "body": {"error": "too many booking attempts"},
            "retry_after": retry_after,
        }

    if (
        not input_data["slot_id"]
        or not input_data["name"]
        or not EMAIL_RE.match(input_data["email"])
    ):
        return {
            "status": 400,
            "body": {"error": "slot_id, name and a valid email are required"},
        }

    slot = conn.execute(
        "SELECT id, start_time <= now() AS started FROM slots WHERE id = %s",
        (input_data["slot_id"],),
    ).fetchone()

    if slot is None:
        return {"status": 404, "body": {"error": "slot not found"}}
    if slot["started"]:
        return {"status": 400, "body": {"error": "slot has already started"}}

    try:
        with conn.transaction():
            row = conn.execute(
                """
                INSERT INTO bookings (id, slot_id, name, email, status, created_at)
                VALUES (%s, %s, %s, %s, 'confirmed', now())
                RETURNING *
                """,
                (
                    str(uuid.uuid4()),
                    input_data["slot_id"],
                    input_data["name"],
                    input_data["email"],
                ),
            ).fetchone()
    except UniqueViolation:
        # The unique partial index makes this safe under concurrent requests.
        return {"status": 409, "body": {"error": "slot is already booked"}}
    except ForeignKeyViolation:
        return {"status": 404, "body": {"error": "slot not found"}}

    return {"status": 201, "body": booking_json(row)}


def result_response(result: dict):
    return JSONResponse(
        status_code=result["status"],
        content=result["body"],
        headers=(
            {"Retry-After": str(result["retry_after"])}
            if result.get("retry_after") is not None
            else None
        ),
    )


def require_api_key(
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
):
    if not API_KEY or not x_api_key or not secrets.compare_digest(x_api_key, API_KEY):
        raise HTTPException(status_code=401, detail="invalid or missing API key")


@asynccontextmanager
async def lifespan(app):
    if not API_KEY:
        raise RuntimeError("API_KEY is required")
    pool.open()
    pool.wait()
    with pool.connection() as conn:
        init_db(conn)
    yield
    pool.close()


app = FastAPI(title="Clinic booking API", lifespan=lifespan)

API_PREFIXES = ("/reset", "/slots", "/bookings")


@app.middleware("http")
async def api_key_middleware(request: Request, call_next):
    # Check the key before the body is parsed, so a missing key is always 401.
    path = request.url.path
    needs_key = any(path == prefix or path.startswith(prefix + "/") for prefix in API_PREFIXES)
    if needs_key and request.method != "OPTIONS":
        provided = request.headers.get("x-api-key")
        if not API_KEY or not provided or not secrets.compare_digest(provided, API_KEY):
            return JSONResponse(
                status_code=401,
                content={"error": "invalid or missing API key"},
            )
    return await call_next(request)


app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["Retry-After"],
)

api = APIRouter(dependencies=[Depends(require_api_key)])


@api.post("/reset")
def reset(payload: ResetIn):
    seen = set()
    clean = []
    for slot in payload.slots:
        if not slot.id.strip() or not slot.provider.strip():
            return error(400, "each slot needs id, provider, start and end")
        if slot.id in seen:
            return error(400, "duplicate slot id")
        seen.add(slot.id)
        clean.append(
            (
                slot.id,
                slot.provider,
                ensure_utc(slot.start),
                ensure_utc(slot.end),
            )
        )

    with pool.connection() as conn:
        with conn.transaction():
            conn.execute("DELETE FROM idempotency")
            conn.execute("DELETE FROM attempts")
            conn.execute("DELETE FROM bookings")
            conn.execute("DELETE FROM slots")
            for slot in clean:
                conn.execute(
                    """
                    INSERT INTO slots (id, provider, start_time, end_time)
                    VALUES (%s, %s, %s, %s)
                    """,
                    slot,
                )

    return {"ok": True}


@api.get("/slots")
def list_slots(provider: str | None = None):
    with pool.connection() as conn:
        rows = conn.execute(
            """
            SELECT s.id, s.provider, s.start_time, s.end_time,
                   EXISTS (
                     SELECT 1 FROM bookings b
                     WHERE b.slot_id = s.id AND b.status = 'confirmed'
                   ) AS booked
            FROM slots s
            WHERE (%s::text IS NULL OR s.provider = %s)
            ORDER BY s.start_time ASC, s.id ASC
            """,
            (provider, provider),
        ).fetchall()

    return {"slots": [slot_json(row) for row in rows]}


@api.post("/bookings")
def create_booking(
    payload: NewBooking,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
):
    input_data = normalize_booking(payload)
    key = (idempotency_key or "").strip() or None
    body_hash = request_hash(input_data)

    with pool.connection() as conn:
        with conn.transaction():
            if key:
                # One request per key at a time, so concurrent retries are safe.
                conn.execute(
                    "SELECT pg_advisory_xact_lock(hashtext(%s))",
                    (f"idem:{key}",),
                )
                saved = conn.execute(
                    """
                    SELECT request_hash, status, body, retry_after
                    FROM idempotency
                    WHERE key = %s
                    """,
                    (key,),
                ).fetchone()

                if saved is not None:
                    if saved["request_hash"] != body_hash:
                        return error(
                            422,
                            "idempotency key reused with a different body",
                        )
                    return JSONResponse(
                        status_code=saved["status"],
                        content=saved["body"],
                        headers=(
                            {"Retry-After": str(saved["retry_after"])}
                            if saved["retry_after"] is not None
                            else None
                        ),
                    )

            result = process_booking(conn, input_data)

            if key:
                conn.execute(
                    """
                    INSERT INTO idempotency
                      (key, request_hash, status, body, retry_after)
                    VALUES (%s, %s, %s, %s, %s)
                    """,
                    (
                        key,
                        body_hash,
                        result["status"],
                        Jsonb(result["body"]),
                        result.get("retry_after"),
                    ),
                )

            return result_response(result)


@api.get("/bookings")
def list_bookings(email: str, limit: str = "10", cursor: str | None = None):
    email = email.strip()
    if not email:
        return error(400, "email is required")

    if not re.fullmatch(r"\d+", limit or ""):
        return error(400, "limit must be between 1 and 50")
    limit_value = int(limit)
    if limit_value < 1 or limit_value > 50:
        return error(400, "limit must be between 1 and 50")

    decoded = None
    if cursor is not None:
        decoded = decode_cursor(cursor)
        if decoded is None:
            return error(400, "invalid cursor")

    with pool.connection() as conn:
        rows = conn.execute(
            """
            SELECT b.*,
                   to_char(
                     b.created_at AT TIME ZONE 'UTC',
                     'YYYY-MM-DD"T"HH24:MI:SS.US"Z"'
                   ) AS cursor_time
            FROM bookings b
            WHERE lower(b.email) = lower(%s)
              AND (
                %s::timestamptz IS NULL
                OR (b.created_at, b.id) > (%s::timestamptz, %s::text)
              )
            ORDER BY b.created_at ASC, b.id ASC
            LIMIT %s
            """,
            (
                email,
                decoded["t"] if decoded else None,
                decoded["t"] if decoded else None,
                decoded["id"] if decoded else None,
                limit_value + 1,
            ),
        ).fetchall()

    page = rows[:limit_value]
    next_cursor = encode_cursor(page[-1]) if len(rows) > limit_value else None

    return {
        "bookings": [booking_json(row) for row in page],
        "next_cursor": next_cursor,
    }


@api.get("/bookings/{booking_id}")
def get_booking(booking_id: str):
    with pool.connection() as conn:
        row = conn.execute(
            "SELECT * FROM bookings WHERE id = %s",
            (booking_id,),
        ).fetchone()

    if row is None:
        return error(404, "booking not found")
    return booking_json(row)


@api.delete("/bookings/{booking_id}")
def cancel_booking(booking_id: str):
    with pool.connection() as conn:
        with conn.transaction():
            row = conn.execute(
                """
                UPDATE bookings
                SET status = 'cancelled'
                WHERE id = %s AND status = 'confirmed'
                RETURNING *
                """,
                (booking_id,),
            ).fetchone()

            if row is not None:
                return booking_json(row)

            existing = conn.execute(
                "SELECT status FROM bookings WHERE id = %s",
                (booking_id,),
            ).fetchone()

    if existing is None:
        return error(404, "booking not found")
    return error(409, "booking is already cancelled")


app.include_router(api)


@app.get("/", include_in_schema=False)
def index():
    return FileResponse(PUBLIC_DIR / "index.html")


@app.exception_handler(RequestValidationError)
async def validation_error_handler(request, exc):
    return JSONResponse(status_code=400, content={"error": "invalid request"})


@app.exception_handler(StarletteHTTPException)
async def http_error_handler(request, exc):
    message = exc.detail
    if exc.status_code == 404:
        message = "not found"
    elif exc.status_code == 405:
        message = "method not allowed"
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": str(message)},
        headers=exc.headers,
    )


@app.exception_handler(Exception)
async def unexpected_error_handler(request, exc):
    logger.exception("Unhandled error", exc_info=exc)
    return JSONResponse(status_code=500, content={"error": "internal server error"})
