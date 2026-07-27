"""Tests for the dashboard WS hub + REST auth."""
from __future__ import annotations

import asyncio
import importlib
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


class FakeWebSocket:
    """Minimal async WS double for the _Hub tests."""

    def __init__(self, *, slow: bool = False, raise_on_send: bool = False) -> None:
        self.slow = slow
        self.raise_on_send = raise_on_send
        self.received: list[dict] = []

    async def send_json(self, payload) -> None:
        if self.raise_on_send:
            raise RuntimeError("simulated network failure")
        if self.slow:
            await asyncio.sleep(5)
        self.received.append(payload)


@pytest.fixture
def hub_mod(tmp_path, monkeypatch):
    monkeypatch.setenv("DEPOYD_CONFIG", str(tmp_path / "config.yaml"))
    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", "x")
    monkeypatch.setenv("LOG_DIR", str(tmp_path / "logs"))
    monkeypatch.setenv("LATE_DASHBOARD_HISTORY_DIR", str(tmp_path / "metrics"))
    monkeypatch.setenv("LATE_AUTH_SECRET", "")
    (tmp_path / "logs").mkdir(parents=True, exist_ok=True)
    (tmp_path / "metrics").mkdir(parents=True, exist_ok=True)
    for m in list(sys.modules.keys()):
        if m.startswith(("config", "events", "deployers", "scheduler", "dashboard", "main")):
            sys.modules.pop(m, None)
    return importlib.import_module("dashboard_ws")


@pytest.mark.asyncio
async def test_hub_broadcast_to_all_clients(hub_mod):
    hub = hub_mod._Hub()
    a, b = FakeWebSocket(), FakeWebSocket()
    await hub.add(a)
    await hub.add(b)
    await hub.broadcast({"hello": 1})
    assert a.received == [{"hello": 1}]
    assert b.received == [{"hello": 1}]


@pytest.mark.asyncio
async def test_hub_broadcast_drops_failing_client(hub_mod):
    hub = hub_mod._Hub()
    good, bad = FakeWebSocket(), FakeWebSocket(raise_on_send=True)
    await hub.add(good)
    await hub.add(bad)
    await hub.broadcast({"ping": 1})
    assert good.received == [{"ping": 1}]
    assert bad.received == []
    assert set(hub.clients) == {good}


@pytest.mark.asyncio
async def test_hub_broadcast_does_not_block_on_slow_client(hub_mod, monkeypatch):
    monkeypatch.setattr(hub_mod, "SEND_TIMEOUT_S", 0.2)
    hub = hub_mod._Hub()
    slow, fast = FakeWebSocket(slow=True), FakeWebSocket()
    await hub.add(slow)
    await hub.add(fast)

    import time
    t0 = time.perf_counter()
    await hub.broadcast({"hi": 1})
    elapsed = time.perf_counter() - t0
    # slow timed out at ~0.2s; fast got it instantly. Total <= ~0.5s.
    assert elapsed < 0.5
    assert fast.received == [{"hi": 1}]
    assert slow.received == []
    assert set(hub.clients) == {fast}


@pytest.mark.asyncio
async def test_hub_add_remove(hub_mod):
    hub = hub_mod._Hub()
    a, b = FakeWebSocket(), FakeWebSocket()
    await hub.add(a)
    await hub.add(b)
    assert set(hub.clients) == {a, b}
    await hub.remove(a)
    assert set(hub.clients) == {b}
    await hub.broadcast({"x": 0})
    assert b.received == [{"x": 0}]
    assert a.received == []


@pytest.fixture
def dashboard_client(monkeypatch, tmp_path):
    """Real FastAPI app (a la test_main_smoke) but with auth mocked off."""
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text("repos:\n  shellx:\n    path: /tmp/x\n    type: shell_only\n")
    monkeypatch.setenv("DEPOYD_CONFIG", str(cfg_path))
    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", "x")
    monkeypatch.setenv("LOG_DIR", str(tmp_path / "logs"))
    monkeypatch.setenv("LATE_DASHBOARD_HISTORY_DIR", str(tmp_path / "metrics"))
    monkeypatch.setenv("LATE_AUTH_SECRET", "sa-secret")
    (tmp_path / "logs").mkdir(parents=True, exist_ok=True)
    (tmp_path / "metrics").mkdir(parents=True, exist_ok=True)
    for m in list(sys.modules.keys()):
        if m.startswith(("config", "events", "deployers", "scheduler", "dashboard", "main")):
            sys.modules.pop(m, None)
    main = importlib.import_module("main")
    return main


def _make_fake_super_admin(monkeypatch, dashboard_client):
    """Replace require_super_admin with one that always returns a fake user."""
    class _User:
        global_role = "super_admin"

    async def fake_auth(request):
        return _User()

    monkeypatch.setattr(
        "dashboard_state.require_super_admin_response", fake_auth
    )


def test_dashboard_state_endpoint_returns_200(dashboard_client, monkeypatch):
    _make_fake_super_admin(monkeypatch, dashboard_client)
    # patch dashboard_state.snapshot so we don't hit /proc etc.
    async def fake_snapshot():
        return {"system": {"cpu_pct": 1}, "gathered_at": "2026-07-27T00:00:00Z"}

    monkeypatch.setattr("dashboard_state.snapshot", fake_snapshot)
    with TestClient(dashboard_client.APP) as client:
        r = client.get("/api/dashboard/state")
    assert r.status_code == 200
    assert r.json()["system"]["cpu_pct"] == 1


def test_dashboard_history_endpoint_validates_range(dashboard_client, monkeypatch):
    _make_fake_super_admin(monkeypatch, dashboard_client)
    async def fake_history(metric, range_seconds):
        return [{"t": 0, "v": 50}]

    monkeypatch.setattr("dashboard_state.history", fake_history)
    with TestClient(dashboard_client.APP) as client:
        r = client.get("/api/dashboard/history?metric=cpu&range=1h")
    assert r.status_code == 200
        # ... and an unknown range
    with TestClient(dashboard_client.APP) as client:
        r = client.get("/api/dashboard/history?metric=cpu&range=bogus")
    assert r.status_code == 400


def test_dashboard_history_endpoint_validates_metric(dashboard_client, monkeypatch):
    _make_fake_super_admin(monkeypatch, dashboard_client)
    with TestClient(dashboard_client.APP) as client:
        r = client.get("/api/dashboard/history?metric=pancake&range=1h")
    assert r.status_code == 400


def test_dashboard_state_unauthorized(dashboard_client, monkeypatch, tmp_path):
    # No LATE_AUTH_SECRET-set shim; we'll force auth to raise 401.
    async def fake_auth(request):
        from fastapi import HTTPException
        raise HTTPException(401, "no token")

    monkeypatch.setattr("dashboard_state.require_super_admin_response", fake_auth)
    with TestClient(dashboard_client.APP) as client:
        r = client.get("/api/dashboard/state")
    assert r.status_code == 401


def test_dashboard_state_forbidden_user(dashboard_client, monkeypatch):
    class _User:
        global_role = "user"  # not super_admin

    async def fake_auth(request):
        from fastapi import HTTPException
        raise HTTPException(403, "super_admin required")

    monkeypatch.setattr("dashboard_state.require_super_admin_response", fake_auth)
    with TestClient(dashboard_client.APP) as client:
        r = client.get("/api/dashboard/state")
    assert r.status_code == 403
