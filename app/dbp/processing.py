"""Job splitting, CPU-intensive task execution and result reduction.

Each job type implements three steps:
  split  - turn a job into independent task payloads (runs in the API)
  run    - CPU-heavy work in Python over one chunk (runs in worker pods)
  reduce - merge the per-task results into the final job result (runs in a worker)
"""
import heapq
import math
import uuid
from collections import Counter

JOB_TYPES = {
    "statistics": "Calculate Statistics",
    "duplicates": "Find Duplicates",
    "join": "Join Records on Key",
    "anomalies": "Detect Anomalies",
    "sort": "Sort Data",
    "report": "Generate Report",
}

TOP_N = 20


def _stream(conn, sql, args=()):
    """Stream rows through a server-side cursor so memory stays bounded."""
    cur = conn.cursor(name="c" + uuid.uuid4().hex)
    cur.itersize = 50000
    try:
        cur.execute(sql, args)
        yield from cur
    finally:
        cur.close()


# ---------------------------------------------------------------- split

def split(job_type, params, min_id, max_id):
    if job_type in {"duplicates", "join"}:
        parts = int(params.get("partitions", 8))
        return [{"part": p, "parts": parts} for p in range(parts)]
    chunk = int(params.get("chunk_size", 250000))
    return [
        {"lo": lo, "hi": min(lo + chunk - 1, max_id)}
        for lo in range(min_id, max_id + 1, chunk)
    ]


# ------------------------------------------------------------------ run

def run(job_type, params, payload, conn):
    return RUNNERS[job_type](params, payload, conn)


def _run_statistics(params, payload, conn):
    n, mean, m2 = 0, 0.0, 0.0  # Welford's online algorithm
    lo_v, hi_v = math.inf, -math.inf
    for (v,) in _stream(conn, "SELECT value FROM records WHERE id BETWEEN %s AND %s",
                        (payload["lo"], payload["hi"])):
        n += 1
        d = v - mean
        mean += d / n
        m2 += d * (v - mean)
        if v < lo_v:
            lo_v = v
        if v > hi_v:
            hi_v = v
    return {"n": n, "mean": mean, "m2": m2, "min": lo_v if n else None, "max": hi_v if n else None}


def _run_duplicates(params, payload, conn):
    counts = Counter()
    for (key,) in _stream(
        conn,
        "SELECT key FROM records WHERE mod(abs(hashtext(key)::bigint), %s) = %s",
        (payload["parts"], payload["part"]),
    ):
        counts[key] += 1
    dups = {k: c for k, c in counts.items() if c > 1}
    return {
        "rows": sum(counts.values()),
        "distinct": len(counts),
        "duplicate_keys": len(dups),
        "duplicate_rows": sum(dups.values()),
        "top": Counter(dups).most_common(TOP_N),
    }


def _run_join(params, payload, conn):
    values_by_key = {}
    for key, value in _stream(
        conn,
        "SELECT key, value FROM records WHERE mod(abs(hashtext(key)::bigint), %s) = %s",
        (payload["parts"], payload["part"]),
    ):
        values_by_key.setdefault(key, []).append(value)

    matched_pairs = 0
    value_difference = 0.0
    for values in values_by_key.values():
        for i, left in enumerate(values):
            for j in range(i + 1, len(values)):
                right = values[j]
                matched_pairs += 1
                value_difference += abs(left - right)

    return {
        "rows_scanned": sum(map(len, values_by_key.values())),
        "keys": len(values_by_key),
        "matched_pairs": matched_pairs,
        "sum_absolute_value_difference": value_difference,
    }


def _run_anomalies(params, payload, conn):
    threshold = float(params.get("z_threshold", 3.0))
    rows = list(_stream(conn, "SELECT id, value FROM records WHERE id BETWEEN %s AND %s",
                        (payload["lo"], payload["hi"])))
    n = len(rows)
    if n < 2:
        return {"n": n, "count": 0, "top": []}
    mean = sum(v for _, v in rows) / n
    std = math.sqrt(sum((v - mean) ** 2 for _, v in rows) / (n - 1)) or 1.0
    found = [(rid, v, (v - mean) / std) for rid, v in rows if abs(v - mean) / std > threshold]
    top = heapq.nlargest(TOP_N, found, key=lambda r: abs(r[2]))
    return {"n": n, "count": len(found), "mean": mean, "std": std, "top": top}


def _run_sort(params, payload, conn):
    column = "key" if params.get("column") == "key" else "value"
    desc = params.get("order", "desc") == "desc"
    rows = list(_stream(conn, f"SELECT id, {column} FROM records WHERE id BETWEEN %s AND %s",
                        (payload["lo"], payload["hi"])))
    rows.sort(key=lambda r: r[1], reverse=desc)  # full sort of the chunk is the CPU work
    return {"n": len(rows), "top": rows[:TOP_N]}


def _run_report(params, payload, conn):
    groups = {}
    for cat, v in _stream(conn, "SELECT category, value FROM records WHERE id BETWEEN %s AND %s",
                          (payload["lo"], payload["hi"])):
        g = groups.get(cat)
        if g is None:
            groups[cat] = [1, v, v, v]
        else:
            g[0] += 1
            g[1] += v
            if v < g[2]:
                g[2] = v
            if v > g[3]:
                g[3] = v
    return {"groups": groups}


RUNNERS = {
    "statistics": _run_statistics,
    "duplicates": _run_duplicates,
    "join": _run_join,
    "anomalies": _run_anomalies,
    "sort": _run_sort,
    "report": _run_report,
}


# --------------------------------------------------------------- reduce

def reduce(job_type, params, results):
    return REDUCERS[job_type](params, results)


def _reduce_statistics(params, results):
    n, mean, m2 = 0, 0.0, 0.0
    lows, highs = [], []
    for r in results:  # Chan et al. parallel variance merge
        if not r["n"]:
            continue
        total = n + r["n"]
        delta = r["mean"] - mean
        m2 += r["m2"] + delta * delta * n * r["n"] / total
        mean += delta * r["n"] / total
        n = total
        lows.append(r["min"])
        highs.append(r["max"])
    var = m2 / (n - 1) if n > 1 else 0.0
    return {"count": n, "mean": mean, "variance": var, "std_dev": math.sqrt(var),
            "min": min(lows) if lows else None, "max": max(highs) if highs else None}


def _reduce_duplicates(params, results):
    top = sorted((t for r in results for t in r["top"]), key=lambda t: -t[1])[:TOP_N]
    return {
        "total_rows": sum(r["rows"] for r in results),
        "distinct_keys": sum(r["distinct"] for r in results),
        "duplicate_keys": sum(r["duplicate_keys"] for r in results),
        "duplicate_rows": sum(r["duplicate_rows"] for r in results),
        "most_repeated": top,
    }


def _reduce_join(params, results):
    return {
        "join_condition": "records.key = records.key (distinct row pairs)",
        "rows_scanned": sum(r["rows_scanned"] for r in results),
        "keys": sum(r["keys"] for r in results),
        "matched_pairs": sum(r["matched_pairs"] for r in results),
        "sum_absolute_value_difference": sum(
            r["sum_absolute_value_difference"] for r in results
        ),
    }


def _reduce_anomalies(params, results):
    top = heapq.nlargest(TOP_N, (t for r in results for t in r["top"]), key=lambda t: abs(t[2]))
    return {
        "rows_scanned": sum(r["n"] for r in results),
        "anomalies": sum(r["count"] for r in results),
        "strongest": [{"id": i, "value": v, "z_score": z} for i, v, z in top],
    }


def _reduce_sort(params, results):
    desc = params.get("order", "desc") == "desc"
    merged = heapq.merge(*(r["top"] for r in results), key=lambda r: r[1], reverse=desc)
    top = [{"id": i, "value": v} for i, v in list(merged)[:TOP_N]]
    return {"rows_sorted": sum(r["n"] for r in results), "column": params.get("column", "value"),
            "order": "desc" if desc else "asc", "top": top}


def _reduce_report(params, results):
    merged = {}
    for r in results:
        for cat, (cnt, tot, lo, hi) in r["groups"].items():
            g = merged.get(cat)
            if g is None:
                merged[cat] = [cnt, tot, lo, hi]
            else:
                g[0] += cnt
                g[1] += tot
                g[2] = min(g[2], lo)
                g[3] = max(g[3], hi)
    rows = [{"category": c, "count": g[0], "sum": g[1], "avg": g[1] / g[0], "min": g[2], "max": g[3]}
            for c, g in sorted(merged.items())]
    return {"total_rows": sum(r["count"] for r in rows), "categories": rows}


def _reduce_seed_dataset(params, results):
    return results[0]


REDUCERS = {
    "statistics": _reduce_statistics,
    "duplicates": _reduce_duplicates,
    "join": _reduce_join,
    "anomalies": _reduce_anomalies,
    "sort": _reduce_sort,
    "report": _reduce_report,
    "seed_dataset": _reduce_seed_dataset,
}
