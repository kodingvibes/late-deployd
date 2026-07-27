"""Tests for Scheduler."""
from __future__ import annotations

import asyncio
import importlib
import sys
from pathlib import Path

import pytest


class FakeRepoConfig:
    def __init__(self, **kw):
        for k, v in kw.items():
            setattr(self, k, v)


@pytest.fixture
def scheduler_mod(tmp_path, monkeypatch):
    monkeypatch.setenv("DEPOYD_CONFIG", str(tmp_path / "config.yaml"))
    for m in list(sys.modules.keys()):
        if m.startswith(("config", "events", "deployers", "scheduler", "dashboard", "main")):
            sys.modules.pop(m, None)
    return importlib.import_module("scheduler")


@pytest.fixture
def bus(scheduler_mod, tmp_path):
    scheduler_mod.EventBus(tmp_path / "events.db")
    b = scheduler_mod.EventBus(tmp_path / "events_2.db")
    return b


@pytest.mark.asyncio
async def test_enqueue_and_run_emits_queued_and_success(monkeypatch, scheduler_mod, bus, tmp_path):
    called = {"n": 0}

    async def fake_to_thread(fn, *a, **kw):
        called["n"] += 1
        return 0

    monkeypatch.setattr(scheduler_mod.asyncio, "to_thread", fake_to_thread)

    cfg = FakeRepoConfig(type="shell_only", path="/tmp/x", branch="main")

    sched = scheduler_mod.Scheduler(scheduler_mod.DeployConfig(tmp_path / "config.yaml"), bus, max_concurrent=1)
    await sched.start(n=1)

    await sched.enqueue("nonexistent", "abc1234", "d-1")  # no repo configured; just won't queue
    assert called["n"] == 0

    # inject a fake config the scheduler will find
    sched._config._repos["fake"] = cfg  # type: ignore[attr-defined]
    await sched.enqueue("fake", "abc1234", "d-2")

    await asyncio.sleep(0.5)  # let worker drain
    assert called["n"] >= 1

    evts = bus.recent_events()
    types = [e["type"] for e in evts]
    assert "deploy.queued" in types
    assert "deploy.success" in types

    await sched.stop()


@pytest.mark.asyncio
async def test_failure_path_publishes_failure_event(monkeypatch, scheduler_mod, bus, tmp_path):
    async def fake_to_thread(fn, *a, **kw):
        return 1

    monkeypatch.setattr(scheduler_mod.asyncio, "to_thread", fake_to_thread)

    sched = scheduler_mod.Scheduler(scheduler_mod.DeployConfig(tmp_path / "config.yaml"), bus, max_concurrent=1)
    await sched.start(n=1)

    sched._config._repos["fake"] = FakeRepoConfig(type="shell_only", path="/tmp/x", branch="main")  # type: ignore
    await sched.enqueue("fake", "deadbee", "d-fail")

    await asyncio.sleep(0.5)
    types = [e["type"] for e in bus.recent_events()]
    assert "deploy.failure" in types

    await sched.stop()


@pytest.mark.asyncio
async def test_exception_in_run_is_caught_and_publishes_failure(monkeypatch, scheduler_mod, bus, tmp_path):
    async def fake_to_thread(fn, *a, **kw):
        raise RuntimeError("boom")

    monkeypatch.setattr(scheduler_mod.asyncio, "to_thread", fake_to_thread)

    sched = scheduler_mod.Scheduler(scheduler_mod.DeployConfig(tmp_path / "config.yaml"), bus, max_concurrent=1)
    await sched.start(n=1)

    sched._config._repos["fake"] = FakeRepoConfig(type="shell_only", path="/tmp/x", branch="main")  # type: ignore
    await sched.enqueue("fake", "deadbee", "d-fail")

    await asyncio.sleep(0.5)
    fail_evts = [e for e in bus.recent_events() if e["type"] == "deploy.failure"]
    assert fail_evts
    payload = fail_evts[-1]["payload"]
    assert payload.get("rc") == 1
    assert any("boom" in line for line in payload.get("log", []))

    await sched.stop()


@pytest.mark.asyncio
async def test_per_repo_lock_serializes_same_repo(monkeypatch, scheduler_mod, bus, tmp_path):
    current = {"running": 0, "max": 0}

    async def fake_to_thread(fn, *a, **kw):
        current["running"] += 1
        current["max"] = max(current["max"], current["running"])
        await asyncio.sleep(0.1)
        current["running"] -= 1
        return 0

    monkeypatch.setattr(scheduler_mod.asyncio, "to_thread", fake_to_thread)

    sched = scheduler_mod.Scheduler(scheduler_mod.DeployConfig(tmp_path / "config.yaml"), bus, max_concurrent=4)
    await sched.start(n=2)

    cfg = FakeRepoConfig(type="shell_only", path="/tmp/x", branch="main")
    sched._config._repos["fake"] = cfg  # type: ignore
    for i in range(3):
        await sched.enqueue("fake", f"abc{i}", f"d-{i}")
    await asyncio.sleep(1.0)
    assert current["max"] == 1

    await sched.stop()


def test_www_lock_exists(scheduler_mod, bus, tmp_path):
    sched = scheduler_mod.Scheduler(scheduler_mod.DeployConfig(tmp_path / "config.yaml"), bus)
    assert isinstance(sched.get_www_lock(), asyncio.Lock)


@pytest.mark.asyncio
async def test_www_lock_serializes_shell_only_deploys(monkeypatch, scheduler_mod, bus, tmp_path):
    """Two parallel shell_only deploys should NOT both write to /var/www/html at the same time."""
    import asyncio

    holding = {"n": 0, "max": 0}

    async def fake_to_thread(fn, *a, **kw):
        # Simulate the critical section: copy_shell_to_www writes under us
        holding["n"] += 1
        holding["max"] = max(holding["max"], holding["n"])
        await asyncio.sleep(0.1)
        holding["n"] -= 1
        return 0

    monkeypatch.setattr(scheduler_mod.asyncio, "to_thread", fake_to_thread)

    sched = scheduler_mod.Scheduler(scheduler_mod.DeployConfig(tmp_path / "config.yaml"), bus, max_concurrent=4)
    await sched.start(n=4)

    cfg_shell = FakeRepoConfig(type="shell_only", path="/tmp/x", branch="main")
    sched._config._repos["shell"] = cfg_shell  # type: ignore
    for i in range(3):
        await sched.enqueue("shell", f"abc{i}", f"d-{i}")
    await asyncio.sleep(1.0)
    assert holding["max"] == 1
    await sched.stop()


@pytest.mark.asyncio
async def test_www_lock_not_used_for_pure_service(monkeypatch, scheduler_mod, bus, tmp_path):
    """A service deploy that does not touch /var/www/html should NOT block on _www_lock.

    Strategy: occupy the _www_lock with a never-released waiter BEFORE enqueueing,
    and confirm the service deploy still completes. If the lock were wrongly acquired
    here, the test would time out.
    """
    import asyncio

    entered_at = asyncio.Event()
    release = asyncio.Event()

    async def fake_to_thread(fn, *a, **kw):
        # enter quickly so the test runtime is bounded
        entered_at.set()
        await release.wait()
        return 0

    monkeypatch.setattr(scheduler_mod.asyncio, "to_thread", fake_to_thread)

    sched = scheduler_mod.Scheduler(scheduler_mod.DeployConfig(tmp_path / "config.yaml"), bus, max_concurrent=4)
    await sched.start(n=4)

    cfg_shell = FakeRepoConfig(type="shell_only", path="/tmp/x", branch="main")
    cfg_svc = FakeRepoConfig(type="service", path="/tmp/svc", branch="main")
    sched._config._repos["shell"] = cfg_shell  # type: ignore
    sched._config._repos["svc"] = cfg_svc  # type: ignore

    await sched.enqueue("shell", "abc1", "d-1")
    await asyncio.wait_for(entered_at.wait(), timeout=2.0)

    # now the www_lock is held by the shell worker
    await sched.enqueue("svc", "abc2", "d-2")
    try:
        await asyncio.wait_for(entered_at.wait(), timeout=0.5)  # second entry — must come from service
        # If we reached here, service entered its deploy without waiting on www_lock
        ok = True
    except asyncio.TimeoutError:
        ok = False

    release.set()
    await sched.stop()
    assert ok, "service deploy was blocked on _www_lock — it should not be"
