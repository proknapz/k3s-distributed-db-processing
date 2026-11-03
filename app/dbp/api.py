import os
from contextlib import contextmanager
from typing import Literal

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from psycopg2.extras import Json, RealDictCursor, execute_values
from pydantic import BaseModel, Field

from . import processing
from .db import connect, init_schema

app = FastAPI(title="Distributed Database Processing Platform")
STATIC = os.path.join(os.path.dirname(__file__), "static")


@app.on_event("startup")
def startup():
    init_schema()


@contextmanager
def cursor():
    conn = connect()
    try:
        with conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
            yield cur
    finally:
        conn.close()


class DatasetRequest(BaseModel):
    rows: int = Field(1_000_000, ge=1000, le=20_000_000)


class JobRequest(BaseModel):
    type: Literal["statistics", "duplicates", "anomalies", "sort", "report"]
    params: dict = {}


@app.get("/", include_in_schema=False)
def index():
    return FileResponse(os.path.join(STATIC, "index.html"))


@app.get("/api/health")
def health():
    with cursor() as cur:
        cur.execute("SELECT 1")
    return {"status": "ok"}


@app.get("/api/job-types")
def job_types():
    return processing.JOB_TYPES


@app.get("/api/dataset")
def dataset_info():
    with cursor() as cur:
        cur.execute("SELECT count(*) AS rows, min(id) AS min_id, max(id) AS max_id FROM records")
        return cur.fetchone()


@app.post("/api/dataset")
def generate_dataset(req: DatasetRequest):
    """Seed the records table (random values with injected outliers and duplicate keys)."""
    with cursor() as cur:
        cur.execute("SELECT count(*) AS n FROM jobs WHERE status IN ('queued', 'running')")
        if cur.fetchone()["n"]:
            raise HTTPException(409, "Wait for running jobs to finish before regenerating data")
        cur.execute("TRUNCATE records RESTART IDENTITY")
        cur.execute(
            """
            INSERT INTO records (category, key, value)
            SELECT 'cat-' || (g % 10),
                   'K' || floor(random() * %(rows)s * 0.8)::bigint,
                   CASE WHEN random() < 0.005 THEN 400 + random() * 600
                        ELSE 50 + (random() + random() + random() - 1.5) * 40 END
            FROM generate_series(1, %(rows)s) g
            """, {"rows": req.rows})
        cur.execute("ANALYZE records")
    return dataset_info()


@app.post("/api/jobs", status_code=201)
def create_job(req: JobRequest):
    params = dict(req.params)
    try:
        chunk = int(params.setdefault("chunk_size", 250000))
        parts = int(params.setdefault("partitions", 8))
    except (TypeError, ValueError):
        raise HTTPException(400, "chunk_size and partitions must be integers")
    if not 1000 <= chunk <= 5_000_000 or not 1 <= parts <= 256:
        raise HTTPException(400, "chunk_size must be 1000-5000000 and partitions 1-256")
    with cursor() as cur:
        cur.execute("SELECT min(id) AS lo, max(id) AS hi FROM records")
        bounds = cur.fetchone()
        if bounds["lo"] is None:
            raise HTTPException(400, "Dataset is empty - generate one first")
        payloads = processing.split(req.type, params, bounds["lo"], bounds["hi"])
        cur.execute("INSERT INTO jobs (type, params) VALUES (%s, %s) RETURNING id",
                    (req.type, Json(params)))
        job_id = cur.fetchone()["id"]
        execute_values(
            cur, "INSERT INTO tasks (job_id, idx, payload) VALUES %s",
            [(job_id, i, Json(p)) for i, p in enumerate(payloads)])
    return {"id": job_id, "tasks": len(payloads)}


JOB_SUMMARY = """
SELECT j.id, j.type, j.status, j.created_at, j.started_at, j.finished_at,
       EXTRACT(EPOCH FROM (COALESCE(j.finished_at, now()) - j.started_at))::float AS elapsed_s,
       count(t.id) AS tasks,
       count(t.id) FILTER (WHERE t.status = 'done') AS done
FROM jobs j LEFT JOIN tasks t ON t.job_id = j.id
"""


@app.get("/api/jobs")
def list_jobs():
    with cursor() as cur:
        cur.execute(JOB_SUMMARY + " GROUP BY j.id ORDER BY j.id DESC LIMIT 25")
        return cur.fetchall()


@app.get("/api/jobs/{job_id}")
def get_job(job_id: int):
    with cursor() as cur:
        cur.execute(JOB_SUMMARY + " WHERE j.id = %s GROUP BY j.id", (job_id,))
        job = cur.fetchone()
        if not job:
            raise HTTPException(404, "Job not found")
        cur.execute("SELECT params, result, error FROM jobs WHERE id = %s", (job_id,))
        job.update(cur.fetchone())
        cur.execute(
            """SELECT COALESCE(node, '(unclaimed)') AS node, count(*) AS tasks,
                      count(*) FILTER (WHERE status = 'done') AS done,
                      COALESCE(sum(duration_ms), 0)::bigint AS busy_ms
               FROM tasks WHERE job_id = %s GROUP BY node ORDER BY node""", (job_id,))
        job["by_node"] = cur.fetchall()
        return job
