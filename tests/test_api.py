#!/usr/bin/env python3
"""End-to-end tests for the clinic booking API.

This uses only the Python standard library, so it needs no extra installs.

Run it against a local or deployed API:

    BASE_URL=http://localhost:8000 API_KEY=your-key python tests/test_api.py

    BASE_URL=https://your-service.onrender.com API_KEY=your-key python tests/test_api.py

WARNING: the suite calls POST /reset, which deletes all bookings and slots.
Only run it against a test database, not against live data.
"""

import base64
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

BASE_URL = os.environ.get("BASE_URL", "http://localhost:8000").rstrip("/")
API_KEY = os.environ.get("API_KEY", "")

if not API_KEY:
    print("Set API_KEY before running, for example:", file=sys.stderr)
    print("  API_KEY=test-key-123 python tests/test_api.py", file=sys.stderr)
    sys.exit(2)


# ---------------------------------------------------------------------------
# Small HTTP helper
# ---------------------------------------------------------------------------


class Response:
    def __init__(self, status, headers, text):
        self.status = status
        self.headers = {name.lower(): value for name, value in headers.items()}
        self.text = text
        try:
            self.body = json.loads(text) if text else None
        except json.JSONDecodeError:
            self.body = None


def call(method, path, body=None, *, raw=None, key=API_KEY, headers=None, timeout=30):
    """Make one request. `body` is encoded as JSON; `raw` is sent verbatim."""
    data = None
    if raw is not None:
        data = raw.encode("utf-8")
    elif body is not None:
        data = json.dumps(body).encode("utf-8")

    request = urllib.request.Request(BASE_URL + path, data=data, method=method)
    if key is not None:
        request.add_header("X-API-Key", key)
    if data is not None:
        request.add_header("Content-Type", "application/json")
    for name, value in (headers or {}).items():
        request.add_header(name, value)

    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return Response(
                response.status,
                dict(response.headers),
                response.read().decode("utf-8", "replace"),
            )
    except urllib.error.HTTPError as exc:
        return Response(
            exc.code,
            dict(exc.headers),
            exc.read().decode("utf-8", "replace"),
        )
    except urllib.error.URLError as exc:
        print(f"Could not reach {BASE_URL}: {exc.reason}", file=sys.stderr)
        raise SystemExit(2)


# ---------------------------------------------------------------------------
# Test runner
# ---------------------------------------------------------------------------


class Runner:
    def __init__(self):
        self.passed = 0
        self.failures = []
        self.group = ""

    def start(self, name):
        self.group = name
        print(f"\n{name}")

    def check(self, label, condition, detail=""):
        if condition:
            self.passed += 1
            print(f"  ok   {label}")
            return
        message = f"{self.group}: {label}"
        if detail:
            message += f" -> {detail}"
        self.failures.append(message)
        print(f"  FAIL {label}" + (f"  [{detail}]" if detail else ""))
        raise AssertionError(label)

    def status(self, label, response, expected):
        self.check(
            label,
            response.status == expected,
            f"expected {expected}, got {response.status}: {response.text[:200]}",
        )
        return response

    def error_shape(self, label, response):
        body = response.body
        self.check(
            label,
            isinstance(body, dict)
            and set(body) == {"error"}
            and isinstance(body["error"], str)
            and bool(body["error"]),
            f"got {response.text[:200]}",
        )
        return response

    def keys(self, label, response, expected):
        body = response.body
        actual = sorted(body) if isinstance(body, dict) else type(body).__name__
        self.check(
            label,
            isinstance(body, dict) and set(body) == set(expected),
            f"expected {sorted(expected)}, got {actual}",
        )
        return response


runner = Runner()
GROUPS = []


def group(function):
    GROUPS.append(function)
    return function


def expect(label, method, path, status, **kwargs):
    response = call(method, path, **kwargs)
    runner.status(label, response, status)
    return response


def expect_error(label, method, path, status, **kwargs):
    response = expect(label, method, path, status, **kwargs)
    runner.error_shape(label + " error shape", response)
    return response


# ---------------------------------------------------------------------------
# Data helpers
# ---------------------------------------------------------------------------


def future(minutes):
    value = datetime.now(timezone.utc) + timedelta(minutes=minutes)
    return value.strftime("%Y-%m-%dT%H:%M:%SZ")


def hours_ago(hours=2):
    value = datetime.now(timezone.utc) - timedelta(hours=hours)
    return value.strftime("%Y-%m-%dT%H:%M:%SZ")


def slot(slot_id, provider, start_minutes, end_minutes=None):
    if end_minutes is None:
        end_minutes = start_minutes + 30
    return {
        "id": slot_id,
        "provider": provider,
        "start": future(start_minutes),
        "end": future(end_minutes),
    }


def reset(slots):
    response = call("POST", "/reset", {"slots": slots})
    runner.status(f"reset setup ({len(slots)} slots)", response, 200)
    return response


def b64(value):
    text = json.dumps(value) if not isinstance(value, str) else value
    return base64.urlsafe_b64encode(text.encode("utf-8")).decode("ascii").rstrip("=")


def parse_iso(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@group
def test_web_auth_and_routing():
    response = call("GET", "/", key=None)
    runner.status("web page returns 200", response, 200)
    runner.check(
        "web page is HTML",
        "text/html" in response.headers.get("content-type", ""),
        response.headers.get("content-type"),
    )
    runner.check("web page contains the title", "Clinic Booking" in response.text)

    # Every API endpoint must reject a missing key with 401 and the error shape.
    for label, method, path, body in [
        ("GET /slots", "GET", "/slots", None),
        ("POST /reset", "POST", "/reset", {"slots": []}),
        (
            "POST /bookings",
            "POST",
            "/bookings",
            {"slot_id": "s1", "name": "A", "email": "a@example.com"},
        ),
        ("GET /bookings", "GET", "/bookings?email=a@example.com", None),
        ("GET /bookings/{id}", "GET", "/bookings/anything", None),
        ("DELETE /bookings/{id}", "DELETE", "/bookings/anything", None),
    ]:
        response = call(method, path, body=body, key=None)
        runner.status(f"{label} without a key -> 401", response, 401)
        runner.error_shape(f"{label} without a key error shape", response)

    # Missing key must still be 401 when the body is empty or malformed.
    response = call("POST", "/bookings", key=None)
    runner.status("POST /bookings without key and body -> 401", response, 401)
    runner.error_shape("empty body auth error shape", response)

    # Wrong and empty keys are rejected too.
    for label, key in [("wrong key", "definitely-not-the-key"), ("empty key", "")]:
        response = call("GET", "/slots", key=key)
        runner.status(f"{label} -> 401", response, 401)
        runner.error_shape(f"{label} error shape", response)

    # The web page and docs are public, not part of the API key check.
    runner.status("GET /docs -> 200", call("GET", "/docs", key=None), 200)
    openapi = expect("GET /openapi.json -> 200", "GET", "/openapi.json", 200, key=None)
    runner.check(
        "OpenAPI title",
        isinstance(openapi.body, dict)
        and openapi.body.get("info", {}).get("title") == "Clinic booking API",
        openapi.text[:200],
    )

    # Unknown routes and methods still use the {"error": "..."} shape.
    response = expect_error("unknown route -> 404", "GET", "/does-not-exist", 404)
    runner.check("unknown route message", response.body["error"] == "not found")
    response = expect_error("wrong method -> 405", "PUT", "/slots", 405)
    runner.check(
        "wrong method message",
        response.body["error"] == "method not allowed",
        response.text,
    )

    # CORS should allow the browser page and expose Retry-After.
    response = call(
        "OPTIONS",
        "/bookings",
        key=None,
        headers={
            "Origin": "https://example.com",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "X-API-Key, Content-Type",
        },
    )
    runner.status("CORS preflight -> 200", response, 200)
    runner.check(
        "CORS preflight allow-origin",
        response.headers.get("access-control-allow-origin") in ("*", "https://example.com"),
        response.headers.get("access-control-allow-origin"),
    )
    runner.check(
        "CORS preflight allows POST",
        "post" in response.headers.get("access-control-allow-methods", "").lower(),
        response.headers.get("access-control-allow-methods"),
    )
    runner.check(
        "CORS preflight allows the API key header",
        "x-api-key" in response.headers.get("access-control-allow-headers", "").lower(),
        response.headers.get("access-control-allow-headers"),
    )

    response = call("GET", "/slots", headers={"Origin": "https://example.com"})
    runner.check(
        "CORS simple response allow-origin",
        response.headers.get("access-control-allow-origin") in ("*", "https://example.com"),
        response.headers.get("access-control-allow-origin"),
    )
    runner.check(
        "CORS exposes Retry-After",
        "retry-after" in response.headers.get("access-control-expose-headers", "").lower(),
        response.headers.get("access-control-expose-headers"),
    )


@group
def test_reset_validation():
    response = reset([])
    runner.keys("reset response shape", response, {"ok"})
    runner.check("reset returns ok=true", response.body["ok"] is True)

    expect_error("reset without slots -> 400", "POST", "/reset", 400, body={})
    expect_error("reset with null slots -> 400", "POST", "/reset", 400, body={"slots": None})
    expect_error(
        "reset with non-array slots -> 400",
        "POST",
        "/reset",
        400,
        body={"slots": "not-a-list"},
    )
    expect_error(
        "reset with non-object slot -> 400",
        "POST",
        "/reset",
        400,
        body={"slots": ["not-an-object"]},
    )
    expect_error(
        "reset slot missing provider -> 400",
        "POST",
        "/reset",
        400,
        body={"slots": [{"id": "x", "start": future(10), "end": future(20)}]},
    )
    expect_error(
        "reset slot blank provider -> 400",
        "POST",
        "/reset",
        400,
        body={"slots": [{"id": "x", "provider": " ", "start": future(10), "end": future(20)}]},
    )
    expect_error(
        "reset slot blank id -> 400",
        "POST",
        "/reset",
        400,
        body={"slots": [{"id": " ", "provider": "Dr X", "start": future(10), "end": future(20)}]},
    )
    expect_error(
        "reset slot missing start -> 400",
        "POST",
        "/reset",
        400,
        body={"slots": [{"id": "x", "provider": "Dr X", "end": future(20)}]},
    )
    expect_error(
        "reset slot invalid start -> 400",
        "POST",
        "/reset",
        400,
        body={
            "slots": [
                {"id": "x", "provider": "Dr X", "start": "not-a-date", "end": future(20)}
            ]
        },
    )
    expect_error(
        "reset duplicate slot id -> 400",
        "POST",
        "/reset",
        400,
        body={
            "slots": [
                {"id": "x", "provider": "Dr X", "start": future(10), "end": future(20)},
                {"id": "x", "provider": "Dr Y", "start": future(30), "end": future(40)},
            ]
        },
    )

    # Past slots are allowed in /reset; booking them is what fails later.
    reset([{"id": "old", "provider": "Dr Old", "start": hours_ago(), "end": hours_ago(1)}])

    # Timezone offsets are accepted and normalised to UTC.
    reset(
        [
            {
                "id": "tz",
                "provider": "Dr TZ",
                "start": "2030-01-01T09:00:00+01:00",
                "end": "2030-01-01T09:30:00+01:00",
            }
        ]
    )
    response = expect("list timezone slot -> 200", "GET", "/slots", 200)
    tz_slot = response.body["slots"][0]
    runner.check(
        "timezone start normalised to UTC",
        tz_slot["start"] == "2030-01-01T08:00:00Z",
        tz_slot["start"],
    )
    runner.check(
        "timezone end normalised to UTC",
        tz_slot["end"] == "2030-01-01T08:30:00Z",
        tz_slot["end"],
    )

    # Reset removes old bookings.
    reset([slot("old-booking-slot", "Dr A", 30, 60)])
    created = expect(
        "booking before reset -> 201",
        "POST",
        "/bookings",
        201,
        body={
            "slot_id": "old-booking-slot",
            "name": "Old Booking",
            "email": "old-booking@example.com",
        },
    )
    old_id = created.body["id"]
    reset([])
    expect_error("booking removed by reset -> 404", "GET", f"/bookings/{old_id}", 404)
    response = expect("slots empty after reset -> 200", "GET", "/slots", 200)
    runner.check("slots empty after reset", response.body["slots"] == [], response.body)


@group
def test_slot_listing_and_filtering():
    reset(
        [
            slot("s2", "Dr Ade", 30, 60),
            slot("s1", "Dr Ade", 60, 90),
            slot("tie-b", "Dr Bello", 90, 120),
            slot("tie-a", "Dr Bello", 90, 120),
            {
                "id": "past",
                "provider": "Dr Ade",
                "start": hours_ago(),
                "end": hours_ago(1),
            },
        ]
    )

    response = expect("list slots -> 200", "GET", "/slots", 200)
    runner.keys("slots response shape", response, {"slots"})
    slots = response.body["slots"]
    runner.check(
        "slots sorted by start then id",
        [item["id"] for item in slots] == ["past", "s2", "s1", "tie-a", "tie-b"],
        [item["id"] for item in slots],
    )
    runner.check(
        "every slot has the expected fields",
        all(set(item) == {"id", "provider", "start", "end", "available"} for item in slots),
        [sorted(item) for item in slots],
    )
    runner.check(
        "every slot is available initially",
        all(item["available"] is True for item in slots),
        slots,
    )
    runner.check(
        "slot timestamps are UTC ISO strings",
        all(item["start"].endswith("Z") and item["end"].endswith("Z") for item in slots),
        slots,
    )
    runner.check(
        "slot end is after start",
        all(parse_iso(item["end"]) > parse_iso(item["start"]) for item in slots),
        slots,
    )

    provider = urllib.parse.quote("Dr Ade")
    response = expect("provider filter -> 200", "GET", f"/slots?provider={provider}", 200)
    runner.check(
        "provider filter returns only Dr Ade",
        [item["id"] for item in response.body["slots"]] == ["past", "s2", "s1"],
        response.body,
    )

    response = expect(
        "unknown provider filter -> 200", "GET", "/slots?provider=Nobody", 200
    )
    runner.check("unknown provider returns no slots", response.body["slots"] == [], response.body)

    # Booking changes availability; cancelling restores it.
    created = expect(
        "book s1 -> 201",
        "POST",
        "/bookings",
        201,
        body={"slot_id": "s1", "name": "Slot User", "email": "slot@example.com"},
    )
    booking_id = created.body["id"]

    response = expect("list slots after booking -> 200", "GET", "/slots", 200)
    availability = {item["id"]: item["available"] for item in response.body["slots"]}
    runner.check("booked slot is unavailable", availability["s1"] is False, availability)
    runner.check("other slots stay available", availability["s2"] is True, availability)
    runner.check("past slot is still listed", availability["past"] is True, availability)

    expect("cancel booked slot -> 200", "DELETE", f"/bookings/{booking_id}", 200)
    response = expect("list slots after cancel -> 200", "GET", "/slots", 200)
    availability = {item["id"]: item["available"] for item in response.body["slots"]}
    runner.check("cancelled slot is available again", availability["s1"] is True, availability)


@group
def test_booking_success_and_validation():
    reset(
        [
            slot("s1", "Dr Ade", 60, 90),
            slot("s2", "Dr Ade", 120, 150),
            slot("s3", "Dr Bello", 180, 210),
            {
                "id": "past",
                "provider": "Dr Ade",
                "start": hours_ago(),
                "end": hours_ago(1),
            },
        ]
    )

    response = expect(
        "book with whitespace and mixed case -> 201",
        "POST",
        "/bookings",
        201,
        body={
            "slot_id": " s1 ",
            "name": "  Ada Obi  ",
            "email": " ADA@Example.COM ",
        },
    )
    booking = response.body
    runner.keys(
        "booking response shape",
        response,
        {"id", "slot_id", "name", "email", "status", "created_at"},
    )
    runner.check("booking id is present", isinstance(booking["id"], str) and booking["id"], booking)
    runner.check("slot_id is trimmed", booking["slot_id"] == "s1", booking)
    runner.check("name is trimmed", booking["name"] == "Ada Obi", booking)
    runner.check("email is trimmed and lowercased", booking["email"] == "ada@example.com", booking)
    runner.check("status is confirmed", booking["status"] == "confirmed", booking)
    runner.check("created_at is UTC", booking["created_at"].endswith("Z"), booking)
    parse_iso(booking["created_at"])
    booking_id = booking["id"]

    response = expect("get booking by id -> 200", "GET", f"/bookings/{booking_id}", 200)
    runner.check("get booking returns the same id", response.body["id"] == booking_id, response.body)
    runner.check("get booking returns confirmed", response.body["status"] == "confirmed", response.body)

    # Missing and invalid values.
    expect_error(
        "missing slot_id -> 400",
        "POST",
        "/bookings",
        400,
        body={"name": "No Slot", "email": "v-noslot@example.com"},
    )
    expect_error(
        "missing name -> 400",
        "POST",
        "/bookings",
        400,
        body={"slot_id": "s2", "email": "v-noname@example.com"},
    )
    expect_error(
        "blank name -> 400",
        "POST",
        "/bookings",
        400,
        body={"slot_id": "s2", "name": "   ", "email": "v-blankname@example.com"},
    )
    expect_error(
        "missing email -> 400",
        "POST",
        "/bookings",
        400,
        body={"slot_id": "s2", "name": "No Email"},
    )
    expect_error(
        "blank email -> 400",
        "POST",
        "/bookings",
        400,
        body={"slot_id": "s2", "name": "Blank Email", "email": "   "},
    )

    for index, bad_email in enumerate(
        ["nope", "a@b", "a b@c.com", "@example.com", "a@.com"]
    ):
        expect_error(
            f"invalid email {bad_email!r} -> 400",
            "POST",
            "/bookings",
            400,
            body={
                "slot_id": "s2",
                "name": "Bad Email",
                "email": f"v-bad-{index}@example.com" if bad_email == "" else bad_email,
            },
        )

    expect_error(
        "non-string slot_id -> 400",
        "POST",
        "/bookings",
        400,
        body={"slot_id": 123, "name": "Bad Type", "email": "v-type1@example.com"},
    )
    expect_error(
        "non-string name -> 400",
        "POST",
        "/bookings",
        400,
        body={"slot_id": "s2", "name": 123, "email": "v-type2@example.com"},
    )
    expect_error(
        "non-string email -> 400",
        "POST",
        "/bookings",
        400,
        body={"slot_id": "s2", "name": "Bad Type", "email": 123},
    )

    # Malformed and missing JSON bodies.
    for label, raw in [
        ("malformed JSON", "{"),
        ("JSON array body", "[]"),
        ("JSON string body", '"text"'),
        ("JSON null body", "null"),
        ("empty body", ""),
    ]:
        expect_error(
            f"{label} -> 400",
            "POST",
            "/bookings",
            400,
            raw=raw,
        )

    expect_error(
        "unknown slot -> 404",
        "POST",
        "/bookings",
        404,
        body={"slot_id": "missing", "name": "No Slot", "email": "v-unknown@example.com"},
    )
    expect_error(
        "past slot -> 400",
        "POST",
        "/bookings",
        400,
        body={"slot_id": "past", "name": "Past", "email": "v-past@example.com"},
    )
    expect_error("unknown booking id -> 404", "GET", "/bookings/not-a-real-id", 404)

    # Duplicate booking: first one wins, second one gets 409.
    first = expect(
        "first booking of s3 -> 201",
        "POST",
        "/bookings",
        201,
        body={"slot_id": "s3", "name": "First", "email": "dup-first@example.com"},
    )
    expect_error(
        "second booking of s3 -> 409",
        "POST",
        "/bookings",
        409,
        body={"slot_id": "s3", "name": "Second", "email": "dup-second@example.com"},
    )
    response = expect("slot still shows first booking", "GET", "/slots", 200)
    availability = {item["id"]: item["available"] for item in response.body["slots"]}
    runner.check("duplicate slot is unavailable", availability["s3"] is False, availability)

    # A cancelled slot can be booked again, with a new booking id.
    expect("cancel first s3 booking -> 200", "DELETE", f"/bookings/{first.body['id']}", 200)
    rebooked = expect(
        "rebook cancelled s3 slot -> 201",
        "POST",
        "/bookings",
        201,
        body={"slot_id": "s3", "name": "Rebooked", "email": "dup-rebook@example.com"},
    )
    runner.check(
        "rebooking gets a different id",
        rebooked.body["id"] != first.body["id"],
        (first.body["id"], rebooked.body["id"]),
    )


@group
def test_cancellation():
    reset([slot("c1", "Dr Cancel", 60, 90), slot("c2", "Dr Cancel", 120, 150)])

    created = expect(
        "book for cancellation -> 201",
        "POST",
        "/bookings",
        201,
        body={"slot_id": "c1", "name": "Cancel Me", "email": "cancel@example.com"},
    )
    booking_id = created.body["id"]

    response = expect("cancel -> 200", "DELETE", f"/bookings/{booking_id}", 200)
    runner.check("cancel response status", response.body["status"] == "cancelled", response.body)
    runner.check("cancel keeps the same id", response.body["id"] == booking_id, response.body)

    response = expect("get cancelled booking -> 200", "GET", f"/bookings/{booking_id}", 200)
    runner.check(
        "lookup shows cancelled status", response.body["status"] == "cancelled", response.body
    )

    expect_error("cancel twice -> 409", "DELETE", f"/bookings/{booking_id}", 409)
    expect_error("cancel unknown booking -> 404", "DELETE", "/bookings/not-a-real-id", 404)

    response = expect("slot free after cancellation", "GET", "/slots", 200)
    availability = {item["id"]: item["available"] for item in response.body["slots"]}
    runner.check("cancelled slot is free", availability["c1"] is True, availability)

    rebooked = expect(
        "rebook cancelled slot -> 201",
        "POST",
        "/bookings",
        201,
        body={"slot_id": "c1", "name": "Rebook", "email": "cancel-rebook@example.com"},
    )
    runner.check("rebook is a new booking", rebooked.body["id"] != booking_id, rebooked.body)


@group
def test_concurrency():
    reset([slot("race", "Dr Race", 60, 90)])

    def attempt(index):
        return call(
            "POST",
            "/bookings",
            {
                "slot_id": "race",
                "name": f"Racer {index}",
                "email": f"race-{index}@example.com",
            },
        )

    with ThreadPoolExecutor(max_workers=20) as pool:
        results = list(pool.map(attempt, range(20)))

    statuses = [result.status for result in results]
    runner.check(
        "20 simultaneous bookings: exactly one 201",
        statuses.count(201) == 1,
        statuses,
    )
    runner.check(
        "20 simultaneous bookings: nineteen 409",
        statuses.count(409) == 19,
        statuses,
    )
    for result in results:
        if result.status == 409:
            runner.error_shape("losing concurrent booking error shape", result)

    winner = next(result for result in results if result.status == 201)
    winner_id = winner.body["id"]

    response = expect("lookup concurrent winner -> 200", "GET", f"/bookings/{winner_id}", 200)
    runner.check("winner is confirmed", response.body["status"] == "confirmed", response.body)

    response = expect("slot unavailable after race", "GET", "/slots", 200)
    runner.check("raced slot unavailable", response.body["slots"][0]["available"] is False, response.body)

    # Two simultaneous cancellations: one succeeds, one conflicts.
    def cancel(_):
        return call("DELETE", f"/bookings/{winner_id}")

    with ThreadPoolExecutor(max_workers=2) as pool:
        cancellations = list(pool.map(cancel, range(2)))
    cancel_statuses = sorted(result.status for result in cancellations)
    runner.check(
        "two simultaneous cancels: one 200 and one 409",
        cancel_statuses == [200, 409],
        cancel_statuses,
    )

    # Different slots can be booked at the same time.
    reset([slot(f"parallel-{index}", "Dr Parallel", 60 + index, 90 + index) for index in range(5)])

    def parallel(index):
        return call(
            "POST",
            "/bookings",
            {
                "slot_id": f"parallel-{index}",
                "name": f"Parallel {index}",
                "email": f"parallel-{index}@example.com",
            },
        )

    with ThreadPoolExecutor(max_workers=5) as pool:
        parallel_results = list(pool.map(parallel, range(5)))
    runner.check(
        "concurrent bookings on different slots all succeed",
        [result.status for result in parallel_results] == [201] * 5,
        [result.status for result in parallel_results],
    )


@group
def test_idempotency():
    reset(
        [
            slot("idem", "Dr Idem", 60, 90),
            slot("idem2", "Dr Idem", 120, 150),
            slot("idem3", "Dr Idem", 180, 210),
            slot("idem4", "Dr Idem", 240, 270),
        ]
    )

    body = {"slot_id": "idem", "name": "Idem User", "email": "idem@example.com"}
    first = expect(
        "idempotent first request -> 201",
        "POST",
        "/bookings",
        201,
        body=body,
        headers={"Idempotency-Key": "key-1"},
    )
    first_id = first.body["id"]
    first_created = first.body["created_at"]

    replay = expect(
        "idempotent replay -> 201",
        "POST",
        "/bookings",
        201,
        body=body,
        headers={"Idempotency-Key": "key-1"},
    )
    runner.check("replay returns the same id", replay.body["id"] == first_id, replay.body)
    runner.check(
        "replay returns the same created_at", replay.body["created_at"] == first_created, replay.body
    )

    normalized_replay = expect(
        "replay with whitespace/case differences -> 201",
        "POST",
        "/bookings",
        201,
        body={
            "slot_id": " idem ",
            "name": "  Idem User  ",
            "email": " IDEM@EXAMPLE.COM ",
        },
        headers={"Idempotency-Key": "key-1"},
    )
    runner.check(
        "normalised replay returns the same id",
        normalized_replay.body["id"] == first_id,
        normalized_replay.body,
    )

    expect_error(
        "same key with a different body -> 422",
        "POST",
        "/bookings",
        422,
        body={"slot_id": "idem", "name": "Different", "email": "idem@example.com"},
        headers={"Idempotency-Key": "key-1"},
    )

    replay_after_error = expect(
        "original body still replays after 422 -> 201",
        "POST",
        "/bookings",
        201,
        body=body,
        headers={"Idempotency-Key": "key-1"},
    )
    runner.check(
        "replay after 422 is still the original booking",
        replay_after_error.body["id"] == first_id,
        replay_after_error.body,
    )

    # An empty idempotency key means "no idempotency".
    empty_key = expect(
        "empty idempotency key books normally -> 201",
        "POST",
        "/bookings",
        201,
        body={"slot_id": "idem2", "name": "Empty Key", "email": "empty-key@example.com"},
        headers={"Idempotency-Key": ""},
    )
    expect_error(
        "empty idempotency key does not replay -> 409",
        "POST",
        "/bookings",
        409,
        body={"slot_id": "idem2", "name": "Empty Key", "email": "empty-key@example.com"},
        headers={"Idempotency-Key": ""},
    )

    # Keys are trimmed.
    trimmed = expect(
        "whitespace-padded idempotency key -> 201",
        "POST",
        "/bookings",
        201,
        body={"slot_id": "idem3", "name": "Trim Key", "email": "trim-key@example.com"},
        headers={"Idempotency-Key": "  trim-key  "},
    )
    trimmed_replay = expect(
        "trimmed key replays -> 201",
        "POST",
        "/bookings",
        201,
        body={"slot_id": "idem3", "name": "Trim Key", "email": "trim-key@example.com"},
        headers={"Idempotency-Key": "trim-key"},
    )
    runner.check(
        "trimmed key returns the same booking",
        trimmed_replay.body["id"] == trimmed.body["id"],
        trimmed_replay.body,
    )

    # Concurrent retries with the same key create one booking.
    reset([slot("same", "Dr Same", 60, 90)])
    concurrent_body = {
        "slot_id": "same",
        "name": "Concurrent Idem",
        "email": "concurrent-idem@example.com",
    }

    def retry(_):
        return call(
            "POST",
            "/bookings",
            concurrent_body,
            headers={"Idempotency-Key": "same-key"},
        )

    with ThreadPoolExecutor(max_workers=10) as pool:
        retries = list(pool.map(retry, range(10)))

    runner.check(
        "concurrent same-key retries all -> 201",
        [result.status for result in retries] == [201] * 10,
        [result.status for result in retries],
    )
    runner.check(
        "concurrent same-key retries share one id",
        len({result.body["id"] for result in retries}) == 1,
        [result.body.get("id") for result in retries],
    )
    response = expect(
        "concurrent retries create one booking",
        "GET",
        "/bookings?email=concurrent-idem@example.com",
        200,
    )
    runner.check(
        "one booking in the list",
        len(response.body["bookings"]) == 1,
        response.body,
    )

    # Idempotency also caches validation errors and conflicts.
    reset([slot("err", "Dr Err", 60, 90), slot("err2", "Dr Err", 120, 150)])
    expect_error(
        "cached validation error -> 400",
        "POST",
        "/bookings",
        400,
        body={"slot_id": "err", "name": "   ", "email": "cached-error@example.com"},
        headers={"Idempotency-Key": "error-key"},
    )
    expect_error(
        "cached validation error replay -> 400",
        "POST",
        "/bookings",
        400,
        body={"slot_id": "err", "name": "   ", "email": "cached-error@example.com"},
        headers={"Idempotency-Key": "error-key"},
    )
    expect_error(
        "cached validation key with a new body -> 422",
        "POST",
        "/bookings",
        422,
        body={"slot_id": "err", "name": "Now Valid", "email": "cached-error@example.com"},
        headers={"Idempotency-Key": "error-key"},
    )

    reset([slot("conflict", "Dr Conflict", 60, 90)])
    conflict_first = expect(
        "conflict setup booking -> 201",
        "POST",
        "/bookings",
        201,
        body={"slot_id": "conflict", "name": "First", "email": "conflict-first@example.com"},
    )
    expect_error(
        "conflict cached -> 409",
        "POST",
        "/bookings",
        409,
        body={"slot_id": "conflict", "name": "Second", "email": "conflict-second@example.com"},
        headers={"Idempotency-Key": "conflict-key"},
    )
    expect_error(
        "conflict replay -> 409",
        "POST",
        "/bookings",
        409,
        body={"slot_id": "conflict", "name": "Second", "email": "conflict-second@example.com"},
        headers={"Idempotency-Key": "conflict-key"},
    )
    expect(
        "cancel conflict booking -> 200",
        "DELETE",
        f"/bookings/{conflict_first.body['id']}",
        200,
    )
    expect_error(
        "cached conflict remains 409 after cancellation",
        "POST",
        "/bookings",
        409,
        body={"slot_id": "conflict", "name": "Second", "email": "conflict-second@example.com"},
        headers={"Idempotency-Key": "conflict-key"},
    )

    # /reset clears idempotency records, so an old key can be used again.
    reset([slot("fresh", "Dr Fresh", 60, 90)])
    expect(
        "old key works after reset -> 201",
        "POST",
        "/bookings",
        201,
        body={"slot_id": "fresh", "name": "Fresh", "email": "fresh@example.com"},
        headers={"Idempotency-Key": "conflict-key"},
    )


@group
def test_pagination():
    reset([slot(f"p{index}", "Dr Page", 60 + index, 90 + index) for index in range(4)])
    created_ids = []
    for index in range(4):
        response = expect(
            f"pagination setup booking {index} -> 201",
            "POST",
            "/bookings",
            201,
            body={
                "slot_id": f"p{index}",
                "name": f"Page {index}",
                "email": "page@example.com",
            },
        )
        created_ids.append(response.body["id"])

    response = expect(
        "default page -> 200",
        "GET",
        "/bookings?email=page@example.com",
        200,
    )
    runner.keys("pagination response shape", response, {"bookings", "next_cursor"})
    runner.check(
        "default page returns all four",
        len(response.body["bookings"]) == 4,
        len(response.body["bookings"]),
    )
    runner.check(
        "default page has no next cursor",
        response.body["next_cursor"] is None,
        response.body["next_cursor"],
    )
    runner.check(
        "pagination returns the same booking ids",
        {item["id"] for item in response.body["bookings"]} == set(created_ids),
        [item["id"] for item in response.body["bookings"]],
    )
    runner.check(
        "booking list items have the booking shape",
        all(
            set(item) == {"id", "slot_id", "name", "email", "status", "created_at"}
            for item in response.body["bookings"]
        ),
        response.body["bookings"],
    )

    # Walk every page with limit=1.
    seen = []
    cursor = None
    pages = 0
    while True:
        query = {"email": "page@example.com", "limit": "1"}
        if cursor:
            query["cursor"] = cursor
        response = expect(
            f"pagination page {pages + 1} -> 200",
            "GET",
            "/bookings?" + urllib.parse.urlencode(query),
            200,
        )
        runner.check(
            f"pagination page {pages + 1} size",
            len(response.body["bookings"]) == 1,
            response.body,
        )
        seen.extend(item["id"] for item in response.body["bookings"])
        pages += 1
        cursor = response.body["next_cursor"]
        if not cursor:
            break

    runner.check("limit=1 returns four pages", pages == 4, pages)
    runner.check(
        "limit=1 has no duplicate bookings",
        len(seen) == 4 and len(set(seen)) == 4,
        seen,
    )
    runner.check("limit=1 returns every booking", set(seen) == set(created_ids), seen)

    # Other page sizes.
    for limit, expected_pages in [(2, 2), (4, 1), (5, 1), (50, 1)]:
        response = expect(
            f"limit={limit} first page -> 200",
            "GET",
            f"/bookings?email=page@example.com&limit={limit}",
            200,
        )
        page_ids = [item["id"] for item in response.body["bookings"]]
        pages = 1
        cursor = response.body["next_cursor"]
        while cursor:
            response = expect(
                f"limit={limit} page {pages + 1} -> 200",
                "GET",
                "/bookings?"
                + urllib.parse.urlencode(
                    {"email": "page@example.com", "limit": str(limit), "cursor": cursor}
                ),
                200,
            )
            page_ids.extend(item["id"] for item in response.body["bookings"])
            pages += 1
            cursor = response.body["next_cursor"]
        runner.check(
            f"limit={limit} page count",
            pages == expected_pages,
            pages,
        )
        runner.check(
            f"limit={limit} returns every booking once",
            set(page_ids) == set(created_ids) and len(page_ids) == 4,
            page_ids,
        )

    # Email matching is case-insensitive and trims whitespace.
    response = expect(
        "uppercase email lookup -> 200",
        "GET",
        "/bookings?email=PAGE@EXAMPLE.COM",
        200,
    )
    runner.check("uppercase email returns all four", len(response.body["bookings"]) == 4, response.body)
    response = expect(
        "whitespace email lookup -> 200",
        "GET",
        "/bookings?"
        + urllib.parse.urlencode({"email": "  page@example.com  "}),
        200,
    )
    runner.check("whitespace email returns all four", len(response.body["bookings"]) == 4, response.body)

    # Cancelled bookings are still part of the person's history.
    expect("cancel one pagination booking -> 200", "DELETE", f"/bookings/{created_ids[0]}", 200)
    response = expect(
        "list after cancellation -> 200",
        "GET",
        "/bookings?email=page@example.com",
        200,
    )
    statuses = {item["id"]: item["status"] for item in response.body["bookings"]}
    runner.check(
        "cancelled booking is still listed",
        statuses.get(created_ids[0]) == "cancelled",
        statuses,
    )

    # Invalid limits.
    for bad_limit in ["0", "51", "-1", "abc", "1.5", ""]:
        expect_error(
            f"invalid limit {bad_limit!r} -> 400",
            "GET",
            "/bookings?email=page@example.com&limit=" + urllib.parse.quote(bad_limit),
            400,
        )
    expect_error("missing email -> 400", "GET", "/bookings", 400)
    expect_error("blank email -> 400", "GET", "/bookings?email=", 400)

    # Invalid cursors.
    invalid_cursors = [
        "not-base64",
        b64({}),
        b64({"t": "not-a-date", "id": "x"}),
        b64({"t": "2030-01-01T00:00:00Z", "id": 123}),
    ]
    for index, cursor in enumerate(invalid_cursors):
        expect_error(
            f"invalid cursor {index} -> 400",
            "GET",
            "/bookings?"
            + urllib.parse.urlencode({"email": "page@example.com", "cursor": cursor}),
            400,
        )

    # A well-formed cursor from before all bookings is accepted.
    response = expect(
        "valid old cursor -> 200",
        "GET",
        "/bookings?"
        + urllib.parse.urlencode(
            {
                "email": "page@example.com",
                "cursor": b64({"t": "2000-01-01T00:00:00Z", "id": "x"}),
            }
        ),
        200,
    )
    runner.check(
        "valid old cursor returns bookings",
        len(response.body["bookings"]) == 4,
        response.body,
    )


@group
def test_rate_limiting():
    reset([slot(f"rl{index}", "Dr Limit", 60 + index, 90 + index) for index in range(6)])

    # Five failed attempts are allowed; the sixth gets 429.
    for index in range(5):
        expect_error(
            f"rate-limit attempt {index + 1} -> 404",
            "POST",
            "/bookings",
            404,
            body={
                "slot_id": "missing",
                "name": "Rate",
                "email": "limit@example.com",
            },
        )

    response = call(
        "POST",
        "/bookings",
        {"slot_id": "missing", "name": "Rate", "email": "limit@example.com"},
        headers={"Origin": "https://example.com"},
    )
    runner.status("sixth attempt -> 429", response, 429)
    runner.error_shape("rate-limit error shape", response)
    retry_after = response.headers.get("retry-after")
    runner.check(
        "429 has Retry-After",
        retry_after is not None and retry_after.isdigit() and 1 <= int(retry_after) <= 60,
        retry_after,
    )
    runner.check(
        "429 exposes Retry-After to CORS",
        "retry-after" in response.headers.get("access-control-expose-headers", "").lower(),
        response.headers.get("access-control-expose-headers"),
    )

    # Email matching is case-insensitive.
    expect_error(
        "different case is still rate limited -> 429",
        "POST",
        "/bookings",
        429,
        body={"slot_id": "missing", "name": "Rate", "email": "LIMIT@EXAMPLE.COM"},
    )

    # Another email is not affected.
    expect(
        "other email unaffected -> 201",
        "POST",
        "/bookings",
        201,
        body={"slot_id": "rl0", "name": "Other", "email": "other@example.com"},
    )

    # Successful bookings count as attempts too.
    reset([slot(f"hit{index}", "Dr Hits", 60 + index, 90 + index) for index in range(6)])
    for index in range(5):
        expect(
            f"successful rate-limit attempt {index + 1} -> 201",
            "POST",
            "/bookings",
            201,
            body={
                "slot_id": f"hit{index}",
                "name": "Hit",
                "email": "success-limit@example.com",
            },
        )
    expect_error(
        "sixth successful-style attempt -> 429",
        "POST",
        "/bookings",
        429,
        body={"slot_id": "hit5", "name": "Hit", "email": "success-limit@example.com"},
    )
    expect(
        "other email can still book the free slot -> 201",
        "POST",
        "/bookings",
        201,
        body={"slot_id": "hit5", "name": "Other", "email": "other-success@example.com"},
    )

    # An idempotent replay of a 429 does not create a new attempt.
    reset([slot("idem-limit", "Dr Limit", 60, 90)])
    for index in range(5):
        expect_error(
            f"idempotent rate-limit attempt {index + 1} -> 404",
            "POST",
            "/bookings",
            404,
            body={"slot_id": "missing", "name": "Rate", "email": "idem-limit@example.com"},
        )
    first_429 = expect_error(
        "idempotent sixth attempt -> 429",
        "POST",
        "/bookings",
        429,
        body={"slot_id": "missing", "name": "Rate", "email": "idem-limit@example.com"},
        headers={"Idempotency-Key": "rate-key"},
    )
    first_retry_after = first_429.headers.get("retry-after")
    replay_429 = expect_error(
        "idempotent 429 replay -> 429",
        "POST",
        "/bookings",
        429,
        body={"slot_id": "missing", "name": "Rate", "email": "idem-limit@example.com"},
        headers={"Idempotency-Key": "rate-key"},
    )
    runner.check(
        "replayed 429 keeps Retry-After",
        replay_429.headers.get("retry-after") == first_retry_after,
        (first_retry_after, replay_429.headers.get("retry-after")),
    )
    expect(
        "other email can book while idem-limit is blocked -> 201",
        "POST",
        "/bookings",
        201,
        body={"slot_id": "idem-limit", "name": "Other", "email": "other-idem-limit@example.com"},
    )

    # /reset clears the attempt window.
    reset([slot("reset-limit", "Dr Limit", 60, 90)])
    expect(
        "reset clears rate limit -> 201",
        "POST",
        "/bookings",
        201,
        body={"slot_id": "reset-limit", "name": "Reset", "email": "limit@example.com"},
    )


@group
def test_empty_results_and_misc():
    reset([])
    response = expect("slots on empty database -> 200", "GET", "/slots", 200)
    runner.check("empty slots list", response.body["slots"] == [], response.body)

    response = expect(
        "unknown email has no bookings -> 200",
        "GET",
        "/bookings?email=nobody@example.com",
        200,
    )
    runner.check("empty bookings list", response.body["bookings"] == [], response.body)
    runner.check("empty bookings next_cursor", response.body["next_cursor"] is None, response.body)

    response = expect(
        "unknown email with limit -> 200",
        "GET",
        "/bookings?email=nobody@example.com&limit=1",
        200,
    )
    runner.check("empty paginated list", response.body["bookings"] == [], response.body)

    # A populated provider filter that matches nothing.
    reset([slot("only", "Dr Only", 60, 90)])
    response = expect(
        "provider filter with no match -> 200",
        "GET",
        "/slots?provider=Missing",
        200,
    )
    runner.check("no matching provider", response.body["slots"] == [], response.body)

    # Reset accepts a slot with a naive timestamp and treats it as UTC.
    reset(
        [
            {
                "id": "naive",
                "provider": "Dr Naive",
                "start": "2030-01-01T09:00:00",
                "end": "2030-01-01T09:30:00",
            }
        ]
    )
    response = expect("naive timestamp accepted -> 200", "GET", "/slots", 200)
    runner.check(
        "naive timestamp is treated as UTC",
        response.body["slots"][0]["start"] == "2030-01-01T09:00:00Z",
        response.body,
    )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main():
    print(f"Testing {BASE_URL}")
    for function in GROUPS:
        runner.start(function.__name__)
        try:
            function()
        except AssertionError:
            pass
        except Exception as exc:  # noqa: BLE001 - report and continue with other groups
            runner.failures.append(
                f"{runner.group}: unexpected {type(exc).__name__}: {exc}"
            )
            print(f"  ERROR {type(exc).__name__}: {exc}")

    print("\n" + "=" * 60)
    if runner.failures:
        print(f"FAILED: {len(runner.failures)} failure(s), {runner.passed} passed")
        for failure in runner.failures:
            print(f"  - {failure}")
        return 1

    print(f"ALL TESTS PASSED ({runner.passed} checks)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
