# Clinic Booking API

A small booking API and web page for the Koya Cohort 4 assessment.

- **API:** Python + FastAPI
- **Database:** PostgreSQL (`psycopg`), so bookings survive restarts and redeploys
- **Web page:** plain HTML/CSS/JS served by the same FastAPI app at `/`

## What is implemented

| Level | Feature |
| --- | --- |
| 1 | List slots, book a slot, look up a booking |
| 2 | `X-API-Key` auth, field validation, past-slot checks |
| 3 | Database-enforced one-booking-per-slot, cancellation |
| 4 | `Idempotency-Key` support, cursor pagination |
| 5 | 5 attempts per email per minute with `Retry-After` |

## Endpoints

Every request needs:

```
X-API-Key: <your key>
```

| Method | Path | Purpose |
| --- | --- | --- |
| `POST` | `/reset` | Replace all slots and clear bookings |
| `GET` | `/slots` | List slots, earliest first; optional `?provider=` |
| `POST` | `/bookings` | Book a slot |
| `GET` | `/bookings?email=&limit=&cursor=` | A person's bookings, oldest first |
| `GET` | `/bookings/{id}` | Get one booking |
| `DELETE` | `/bookings/{id}` | Cancel a booking |

Booking body:

```json
{
  "slot_id": "s1",
  "name": "Ada Obi",
  "email": "ada@example.com"
}
```

Optional header on `POST /bookings`:

```
Idempotency-Key: any-unique-string
```

Errors always look like:

```json
{ "error": "message" }
```

## Run locally

You need Python 3.10+ and PostgreSQL.

```bash
cd clinic-booking
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt

cp .env.example .env
# edit .env with your local DATABASE_URL and API_KEY

uvicorn main:app --host 0.0.0.0 --port 8000
```

On startup the app creates its tables from `schema.sql`. Open
<http://localhost:8000>. The page asks for the API key, then loads slots.

If you do not already have Postgres, the quickest local option is Docker:

```bash
docker run -d --name clinic-pg \
  -e POSTGRES_PASSWORD=postgres \
  -e POSTGRES_DB=clinic_booking \
  -p 5432:5432 postgres:16-alpine
```

Then use this in `.env`:

```
DATABASE_URL=postgresql://postgres:postgres@localhost:5432/clinic_booking
```

Quick smoke test:

```bash
curl -X POST http://localhost:8000/reset \
  -H "X-API-Key: $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"slots":[{"id":"s1","provider":"Dr Ade","start":"2026-12-10T09:00:00Z","end":"2026-12-10T09:30:00Z"}]}'

curl http://localhost:8000/slots -H "X-API-Key: $API_KEY"

curl -X POST http://localhost:8000/bookings \
  -H "X-API-Key: $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"slot_id":"s1","name":"Ada Obi","email":"ada@example.com"}'
```

## Test every endpoint

`tests/test_api.py` is a self-contained end-to-end suite that uses only the
Python standard library. It covers auth, all endpoints, validation, concurrency,
cancellation, idempotency, pagination, rate limiting, CORS, and error shapes.

Start the API locally first, then run:

```bash
API_KEY=your-local-key python tests/test_api.py
```

Against the deployed API:

```bash
BASE_URL=https://YOUR-SERVICE.onrender.com \
API_KEY=YOUR_API_KEY \
python tests/test_api.py
```

On PowerShell:

```powershell
$env:BASE_URL = "https://YOUR-SERVICE.onrender.com"
$env:API_KEY = "YOUR_API_KEY"
python tests/test_api.py
```

Expected final line:

```
ALL TESTS PASSED (365 checks)
```

> **Warning:** the suite calls `POST /reset`, which deletes all bookings and
> slots. Run it against a test database, not against live data. Running it
> before submission is fine.

## Push to GitHub

Create an empty repository on GitHub first, then run:

```bash
cd clinic-booking
git init
git add .
git commit -m "Build clinic booking API"
git branch -M main
git remote add origin https://github.com/YOUR_USERNAME/clinic-booking.git
git push -u origin main
```

## Deploy

The repository includes `render.yaml`, which creates both the web service and a
free PostgreSQL database.

1. Sign in at <https://render.com>.
2. **New → Blueprint**.
3. Connect your GitHub repository and apply the Blueprint.
4. When prompted, set `API_KEY` to a long random key you choose. Render creates
   `DATABASE_URL` for you.
5. Wait for the deploy and copy the `https://...onrender.com` URL.

Render's free web service sleeps after inactivity, so the first request may take
a few seconds. The database keeps the data.

If you prefer to use an external Postgres (for example Neon), delete the
`databases:` block from `render.yaml`, then set `DATABASE_URL` manually in the
Render dashboard.

### Smoke test the deployment

```bash
BASE=https://YOUR-SERVICE.onrender.com
KEY=YOUR_API_KEY

curl -X POST "$BASE/reset" \
  -H "X-API-Key: $KEY" \
  -H "Content-Type: application/json" \
  -d '{"slots":[{"id":"s1","provider":"Dr Ade","start":"2026-12-10T09:00:00Z","end":"2026-12-10T09:30:00Z"}]}'

curl "$BASE/slots" -H "X-API-Key: $KEY"
```

Then open `https://YOUR-SERVICE.onrender.com` in a browser, enter the same API key, and book a slot.

## Notes

- `POST /reset` is useful for testing. It deletes all bookings, attempts and idempotency records, then replaces the slots.
- The unique partial index `one_confirmed_booking_per_slot` is what prevents double booking under concurrent requests.
- The web page stores the API key in the browser's `localStorage`.
- FastAPI also serves interactive docs at `/docs` (the assessment only needs the endpoints above).
