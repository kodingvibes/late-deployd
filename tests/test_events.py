"""Tests for EventBus."""
from __future__ import annotations

import asyncio
import importlib
import sqlite3
import sys
import time
from pathlib import Path

import pytest


@pytest.fixture
def events_mod(tmp_path, monkeypatch):
    monkeypatch.setattr(events_module_default := sys.modules.get("events"), "DB_PATH", tmp_path / "events.db") if False else None
    for m in list(sys.modules.keys()):
        if m.startswith(("config", "events", "deployers", "scheduler", "main")):
            sys.modules.pop(m, None)
    mod = importlib.import_module("events")
    monkeypatch.setattr(mod, "RETENTION_SECONDS", 30 * 24 * 3600, raising=False)
    return mod


def test_publish_persists_to_sqlite(events_mod, tmp_path):
    bus = events_mod.EventBus(tmp_path / "events.db")
    bus.publish("deploy.queued", repo="r1", delivery="d-1", payload={"after": "abc"})
    with sqlite3.connect(tmp_path / "events.db") as conn:
        rows = conn.execute("SELECT type, repo, delivery, payload FROM events").fetchall()
    assert len(rows) == 1
    typ, repo, delivery, payload = rows[0]
    assert typ == "deploy.queued"
    assert repo == "r1"
    assert delivery == "d-1"
    assert '"after"' in payload


def test_publish_assigns_id_and_timestamp(events_mod, tmp_path):
    bus = events_mod.EventBus(tmp_path / "events.db")
    before = time.time()
    bus.publish("webhook.received")
    after = time.time()
    rows = bus.recent_events()
    assert len(rows) == 1
    e = rows[0]
    assert e["event_id"].startswith("evt_")
    assert before <= e["timestamp"] <= after


def test_recent_events_orders_newest_first_in_ui_calls(events_mod, tmp_path):
    bus = events_mod.EventBus(tmp_path / "events.db")
    bus.publish("a")
    bus.publish("b")
    rows = bus.recent_events()
    # _load ordering returns rows oldest-first internally then reversed in recent_events
    assert [r["type"] for r in rows] == ["a", "b"]


def test_recent_events_limit_and_filter(events_mod, tmp_path):
    bus = events_mod.EventBus(tmp_path / "events.db")
    for i in range(5):
        bus.publish("deploy.queued", repo=f"r{i % 2}", delivery=str(i))
    rows = bus.recent_events(limit=3)
    assert len(rows) == 3
    rows2 = bus.recent_events(repo="r0")
    assert len(rows2) == 3
    rows3 = bus.recent_events(type="deploy.queued")
    assert len(rows3) == 5


def test_retention_evicts_old(events_mod, tmp_path, monkeypatch):
    monkeypatch.setattr(events_mod, "RETENTION_SECONDS", 1)
    bus = events_mod.EventBus(tmp_path / "events.db")
    bus.publish("old")
    with sqlite3.connect(tmp_path / "events.db") as conn:
        conn.execute("UPDATE events SET timestamp = ?", (time.time() - 100,))
        conn.commit()
    bus.publish("new")
    rows = bus.recent_events()
    assert [r["type"] for r in rows] == ["new"]


@pytest.mark.asyncio
async def test_subscribe_receives_published_events(events_mod, tmp_path):
    bus = events_mod.EventBus(tmp_path / "events.db")
    loop = asyncio.get_running_loop()
    bus.set_loop(loop)
    q = await bus.subscribe()
    bus.publish("hello", repo="r")
    msg = await asyncio.wait_for(q.get(), timeout=2.0)
    assert msg["type"] == "hello"
    bus.unsubscribe(q)


@pytest.mark.asyncio
async def test_unsubscribe_stops_pushing(events_mod, tmp_path):
    bus = events_mod.EventBus(tmp_path / "events.db")
    loop = asyncio.get_running_loop()
    bus.set_loop(loop)
    q = await bus.subscribe()
    bus.unsubscribe(q)
    bus.publish("after-unsub")
    # queue should not receive
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(q.get(), timeout=0.2)


def test_publish_with_no_payload_succeeds(events_mod, tmp_path):
    bus = events_mod.EventBus(tmp_path / "events.db")
    bus.publish("event.x")
    bus.publish("event.y", payload=None)
    rows = bus.recent_events()
    assert len(rows) == 2
    assert all(r["payload"] == {} for r in rows)


def test_event_id_is_unique(events_mod, tmp_path):
    bus = events_mod.EventBus(tmp_path / "events.db")
    for _ in range(10):
        bus.publish("dup")
    with sqlite3.connect(tmp_path / "events.db") as conn:
        rows = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        distinct = conn.execute("SELECT COUNT(DISTINCT event_id) FROM events").fetchone()[0]
    assert rows == 10
    assert distinct == 10


def test_latest_deploys_returns_most_recent_per_repo(events_mod, tmp_path):
    bus = events_mod.EventBus(tmp_path / "events.db")
    bus.publish("deploy.queued", repo="a", delivery="d1", payload={"after": "1"})
    bus.publish("deploy.success", repo="a", delivery="d1", payload={"after": "1"})
    bus.publish("deploy.failure", repo="b", delivery="d2", payload={"after": "2", "rc": 1})
    bus.publish("deploy.started", repo="c", delivery="d3", payload={"after": "3"})

    latest = bus.latest_deploys()
    assert latest["a"]["type"] == "deploy.success"
    assert latest["b"]["type"] == "deploy.failure"
    assert latest["c"]["type"] == "deploy.started"
    assert latest["a"]["after"] == "1"


def test_latest_deploys_ignores_non_deploy_events(events_mod, tmp_path):
    bus = events_mod.EventBus(tmp_path / "events.db")
    bus.publish("poll.started", payload={"repos": 1})
    bus.publish("webhook.received", repo="r", delivery="x")
    assert bus.latest_deploys() == {}
