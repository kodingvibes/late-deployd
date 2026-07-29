from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import time
import uuid
from pathlib import Path
from threading import Lock
from typing import Any, Optional

RETENTION_SECONDS = 30 * 86400
DB_PATH = Path(os.environ.get("DEPOYD_DB_PATH", "/root/.deployd/events.db"))


class EventBus:
    def __init__(self, db_path: str | Path = DB_PATH):
        self._db_path = Path(db_path)
        self._subscribers: list[asyncio.Queue] = []
        self._lock = Lock()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._init_db()

    def set_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    def _init_db(self) -> None:
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with sqlite3.connect(str(self._db_path)) as conn:
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS events (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        event_id TEXT UNIQUE,
                        type TEXT NOT NULL,
                        timestamp REAL NOT NULL,
                        repo TEXT DEFAULT '',
                        delivery TEXT DEFAULT '',
                        payload TEXT DEFAULT '{}'
                    )
                """)
                conn.execute("CREATE INDEX IF NOT EXISTS idx_events_type ON events(type)")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_events_repo ON events(repo)")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_events_ts ON events(timestamp)")
        except Exception:
            pass

    def publish(self, type: str, repo: str = "", delivery: str = "", payload: Optional[dict] = None) -> None:
        event = {
            "event_id": f"evt_{uuid.uuid4().hex[:12]}",
            "type": type,
            "timestamp": time.time(),
            "repo": repo,
            "delivery": delivery,
            "payload": payload or {},
        }
        try:
            with sqlite3.connect(str(self._db_path)) as conn:
                conn.execute(
                    "INSERT OR IGNORE INTO events (event_id, type, timestamp, repo, delivery, payload) VALUES (?, ?, ?, ?, ?, ?)",
                    (event["event_id"], type, event["timestamp"], repo, delivery, json.dumps(payload or {})),
                )
                conn.execute("DELETE FROM events WHERE timestamp < ?", (time.time() - RETENTION_SECONDS,))
        except Exception:
            pass
        loop = self._loop
        if loop is not None and loop.is_running():
            with self._lock:
                for q in self._subscribers:
                    loop.call_soon_threadsafe(q.put_nowait, event)

    async def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue()
        with self._lock:
            self._subscribers.append(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        with self._lock:
            self._subscribers = [s for s in self._subscribers if s is not q]

    def recent_events(self, limit: int = 50, repo: Optional[str] = None, type: Optional[str] = None) -> list[dict]:
        try:
            with sqlite3.connect(str(self._db_path)) as conn:
                where = []
                params: list[Any] = []
                if repo:
                    where.append("repo = ?")
                    params.append(repo)
                if type:
                    where.append("type = ?")
                    params.append(type)
                where_clause = " AND ".join(where) if where else "1"
                rows = conn.execute(
                    f"SELECT event_id, type, timestamp, repo, delivery, payload FROM events WHERE {where_clause} ORDER BY id DESC LIMIT ?",
                    [*params, limit],
                ).fetchall()
        except Exception:
            return []
        out = []
        for r in reversed(rows):
            try:
                payload = json.loads(r[5]) if r[5] else {}
            except (json.JSONDecodeError, TypeError):
                payload = {}
            out.append({
                "event_id": r[0],
                "type": r[1],
                "timestamp": r[2],
                "repo": r[3],
                "delivery": r[4],
                "payload": payload,
            })
        return out