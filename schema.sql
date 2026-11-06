CREATE TABLE IF NOT EXISTS slots (
  id TEXT PRIMARY KEY,
  provider TEXT NOT NULL,
  start_time TIMESTAMPTZ NOT NULL,
  end_time TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS bookings (
  id TEXT PRIMARY KEY,
  slot_id TEXT NOT NULL REFERENCES slots(id) ON DELETE CASCADE,
  name TEXT NOT NULL,
  email TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('confirmed', 'cancelled')),
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- The database, not the application, enforces one confirmed booking per slot.
CREATE UNIQUE INDEX IF NOT EXISTS one_confirmed_booking_per_slot
  ON bookings (slot_id) WHERE status = 'confirmed';

CREATE INDEX IF NOT EXISTS bookings_email_created_idx
  ON bookings (lower(email), created_at, id);

CREATE TABLE IF NOT EXISTS attempts (
  id BIGSERIAL PRIMARY KEY,
  email TEXT NOT NULL,
  attempted_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS attempts_email_time_idx
  ON attempts (email, attempted_at);

CREATE TABLE IF NOT EXISTS idempotency (
  key TEXT PRIMARY KEY,
  request_hash TEXT NOT NULL,
  status INTEGER NOT NULL,
  body JSONB NOT NULL,
  retry_after INTEGER
);
