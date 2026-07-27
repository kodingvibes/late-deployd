from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path

DB_PATH = Path("/var/lib/late-dashboard/metrics/history.db")
RANGES: dict[str, int] = {
    "1m": 60,
    "5m": 5 * 60,
    "15m": 15 * 60,
    "30m": 30 * 60,
    "1h": 3600,
    "6h": 6 * 3600,
    "24h": 24 * 3600,
    "7d": 7 * 24 * 3600,
    "30d": 30 * 24 * 3600,
}
RETENTION_SECONDS = 30 * 24 * 3600
DOWNSAMPLE_TARGET = 1000

_local = threading.local()


def _conn() -> sqlite3.Connection:
    if not hasattr(_local, "conn") or _local.conn is None:
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        c = sqlite3.connect(str(DB_PATH), timeout=5)
        c.execute(
            "CREATE TABLE IF NOT EXISTS samples ("
            "  metric TEXT NOT NULL,"
            "  t REAL NOT NULL,"
            "  v REAL NOT NULL"
            ")"
        )
        c.execute(
            "CREATE INDEX IF NOT EXISTS idx_samples_metric_t ON samples(metric, t)"
        )
        _local.conn = c
    return _local.conn


def append_sample(metric: str, value: float, t: float | None = None) -> None:
    if t is None:
        t = time.time()
    try:
        c = _conn()
        c.execute("INSERT INTO samples (metric, t, v) VALUES (?, ?, ?)", (metric, t, value))
        c.commit()
    except Exception:
        pass


def read_samples(metric: str, range_seconds: int) -> list[dict]:
    now = time.time()
    cutoff = now - range_seconds
    try:
        c = _conn()
        rows = c.execute(
            "SELECT t, v FROM samples WHERE metric = ? AND t >= ? AND t <= ? ORDER BY t",
            (metric, cutoff, now),
        ).fetchall()
    except Exception:
        return []
    raw = [{"t": r[0], "v": r[1]} for r in rows]
    if len(raw) <= DOWNSAMPLE_TARGET:
        return raw
    n = len(raw)
    bucket = (n + DOWNSAMPLE_TARGET - 1) // DOWNSAMPLE_TARGET
    out = []
    for i in range(0, n, bucket):
        chunk = raw[i : i + bucket]
        if not chunk:
            continue
        t = chunk[len(chunk) // 2]["t"]
        v = sum(c["v"] for c in chunk) / len(chunk)
        out.append({"t": t, "v": v})
    return out


def roll() -> None:
    cutoff = time.time() - RETENTION_SECONDS
    try:
        c = _conn()
        c.execute("DELETE FROM samples WHERE t < ?", (cutoff,))
        c.execute("VACUUM")
        c.commit()
    except Exception:
        pass
