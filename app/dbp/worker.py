import os
import time
import traceback

from psycopg2.extras import Json

from . import processing
from .db import connect, init_schema

POD = os.getenv("POD_NAME", "worker-local")
NODE = os.getenv("NODE_NAME", "unknown")
POLL_SECONDS = float(os.getenv("POLL_SECONDS", "1"))
MAX_ATTEMPTS = int(os.getenv("MAX_ATTEMPTS", "3"))
STALE_SECONDS = int(os.getenv("STALE_SECONDS", "300"))

CLAIM_SQL = """
UPDATE tasks SET status = 'running', worker = %s, node = %s,
                 claimed_at = now(), attempts = attempts + 1
WHERE id = (
    SELECT t.id FROM tasks t
    JOIN jobs j ON j.id = t.job_id
    WHERE t.status = 'pending' AND j.status IN ('queued', 'running')
    ORDER BY t.id
    FOR UPDATE OF t SKIP LOCKED
    LIMIT 1
)
RETURNING id, job_id, payload
"""


def claim(conn):
    with conn, conn.cursor() as cur:
        cur.execute(CLAIM_SQL, (POD, NODE))
        row = cur.fetchone()
        if not row:
            return None
        task_id, job_id, payload = row
        cur.execute(
            "UPDATE jobs SET status = 'running', started_at = now() "
            "WHERE id = %s AND status = 'queued'", (job_id,))
        cur.execute("SELECT type, params FROM jobs WHERE id = %s", (job_id,))
        job_type, params = cur.fetchone()
        return task_id, job_id, job_type, params, payload


def complete(conn, task_id, result, started):
    with conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE tasks SET status = 'done', result = %s, finished_at = now(), "
            "duration_ms = %s WHERE id = %s",
            (Json(result), int((time.monotonic() - started) * 1000), task_id))


def fail(conn, task_id, job_id, error):
    with conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE tasks SET status = CASE WHEN attempts >= %s THEN 'failed' ELSE 'pending' END, "
            "error = %s WHERE id = %s RETURNING status", (MAX_ATTEMPTS, error[-2000:], task_id))
        if cur.fetchone()[0] == "failed":
            cur.execute(
                "UPDATE jobs SET status = 'failed', error = %s, finished_at = now() "
                "WHERE id = %s AND status IN ('queued', 'running')",
                (f"task {task_id} failed: {error[-500:]}", job_id))


def finalize(conn, job_id):
    """Reduce results once every task of the job is done."""
    with conn, conn.cursor() as cur:
        cur.execute("SELECT type, params FROM jobs WHERE id = %s AND status = 'running' FOR UPDATE",
                    (job_id,))
        job = cur.fetchone()
        if not job:
            return
        cur.execute("SELECT count(*) FROM tasks WHERE job_id = %s AND status <> 'done'", (job_id,))
        if cur.fetchone()[0]:
            return
        cur.execute("SELECT result FROM tasks WHERE job_id = %s ORDER BY idx", (job_id,))
        results = [r[0] for r in cur.fetchall()]
        final = processing.reduce(job[0], job[1], results)
        cur.execute(
            "UPDATE jobs SET status = 'done', result = %s, finished_at = now() WHERE id = %s",
            (Json(final), job_id))


def requeue_stale(conn):
    """Recover tasks held by pods that died mid-task."""
    with conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE tasks SET status = CASE WHEN attempts >= %s THEN 'failed' ELSE 'pending' END, "
            "error = 'worker lost' "
            "WHERE status = 'running' AND claimed_at < now() - make_interval(secs => %s) "
            "RETURNING job_id, status", (MAX_ATTEMPTS, STALE_SECONDS))
        for job_id, status in cur.fetchall():
            if status == "failed":
                cur.execute("UPDATE jobs SET status = 'failed', error = 'task lost', "
                            "finished_at = now() WHERE id = %s AND status IN ('queued', 'running')",
                            (job_id,))


def handle_seed(conn, params, payload):
    """Handle the system job for generating the dataset."""
    rows = params.get("rows", 1_000_000)
    with conn.cursor() as cur:
        cur.execute("TRUNCATE records RESTART IDENTITY")
        cur.execute(
            """
            INSERT INTO records (category, key, value)
            SELECT 'cat-' || (g %% 10),
                   'K' || floor(random() * %(rows)s * 0.8)::bigint,
                   CASE WHEN random() < 0.005 THEN 400 + random() * 600
                        ELSE 50 + (random() + random() + random() - 1.5) * 40 END
            FROM generate_series(1, %(rows)s) g
            """, {"rows": rows})
    return {"rows": rows, "status": "seeded"}


def main():
    init_schema()
    print(f"worker {POD} on node {NODE} started", flush=True)
    conn = connect()
    last_requeue = 0.0
    while True:
        try:
            if time.monotonic() - last_requeue > 30:
                requeue_stale(conn)
                last_requeue = time.monotonic()
            task = claim(conn)
            if not task:
                time.sleep(POLL_SECONDS)
                continue
            task_id, job_id, job_type, params, payload = task
            started = time.monotonic()
            print(f"task {task_id} (job {job_id}, {job_type}) {payload}", flush=True)
            try:
                if job_type == "seed_dataset":
                    result = handle_seed(conn, params, payload)
                else:
                    result = processing.run(job_type, params, payload, conn)
                conn.commit()
            except Exception:
                conn.rollback()
                fail(conn, task_id, job_id, traceback.format_exc())
                continue
            complete(conn, task_id, result, started)
            finalize(conn, job_id)
        except Exception:
            traceback.print_exc()
            try:
                conn.close()
            except Exception:
                pass
            time.sleep(2)
            conn = connect()


if __name__ == "__main__":
    main()
