import os
import time

import psycopg2

SCHEMA = """
CREATE TABLE IF NOT EXISTS records (
    id        BIGSERIAL PRIMARY KEY,
    category  TEXT NOT NULL,
    key       TEXT NOT NULL,
    value     DOUBLE PRECISION NOT NULL
);

CREATE TABLE IF NOT EXISTS jobs (
    id          SERIAL PRIMARY KEY,
    type        TEXT NOT NULL,
    params      JSONB NOT NULL DEFAULT '{}',
    status      TEXT NOT NULL DEFAULT 'queued',   -- queued | running | done | failed
    result      JSONB,
    error       TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    started_at  TIMESTAMPTZ,
    finished_at TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS tasks (
    id          BIGSERIAL PRIMARY KEY,
    job_id      INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    idx         INTEGER NOT NULL,
    payload     JSONB NOT NULL,
    status      TEXT NOT NULL DEFAULT 'pending',  -- pending | running | done | failed
    attempts    INTEGER NOT NULL DEFAULT 0,
    worker      TEXT,
    node        TEXT,
    claimed_at  TIMESTAMPTZ,
    finished_at TIMESTAMPTZ,
    duration_ms INTEGER,
    result      JSONB,
    error       TEXT
);
CREATE INDEX IF NOT EXISTS tasks_pending_idx ON tasks (id) WHERE status = 'pending';
CREATE INDEX IF NOT EXISTS tasks_job_idx ON tasks (job_id);
"""


def connect():
    return psycopg2.connect(
        host=os.getenv("PGHOST", "postgres"),
        port=int(os.getenv("PGPORT", "5432")),
        dbname=os.getenv("PGDATABASE", "dbp"),
        user=os.getenv("PGUSER", "dbp"),
        password=os.getenv("PGPASSWORD", "dbp"),
        connect_timeout=5,
    )


def wait_for_db(retries=60, delay=2):
    for attempt in range(retries):
        try:
            conn = connect()
            conn.close()
            return
        except psycopg2.OperationalError as exc:
            print(f"waiting for PostgreSQL ({attempt + 1}/{retries}): {exc}".strip(), flush=True)
            time.sleep(delay)
    raise RuntimeError("PostgreSQL is not reachable")


def init_schema():
    wait_for_db()
    conn = connect()
    try:
        with conn, conn.cursor() as cur:
            # Serialises schema creation when several pods start at once.
            cur.execute("SELECT pg_advisory_xact_lock(424242)")
            cur.execute(SCHEMA)
    finally:
        conn.close()
