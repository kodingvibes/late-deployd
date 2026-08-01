"""Tests for the repo Poller."""
from __future__ import annotations

import asyncio
import importlib
import sys
from pathlib import Path

import pytest


class FakeRepoConfig:
    def __init__(self, name="fake", **kw):
        self.name = name
        for k, v in kw.items():
            setattr(self, k, v)


@pytest.fixture
def poller_mod(tmp_path, monkeypatch):
    monkeypatch.setenv("DEPOYD_CONFIG", str(tmp_path / "config.yaml"))
    for m in list(sys.modules.keys()):
        if m.startswith(("config", "events", "deployers", "scheduler", "poller", "main")):
            sys.modules.pop(m, None)
    return importlib.import_module("poller")


@pytest.fixture
def bus(poller_mod, tmp_path):
    return poller_mod.EventBus(tmp_path / "events.db")


@pytest.mark.asyncio
async def test_tick_with_no_repos_does_not_crash(poller_mod, bus, tmp_path, monkeypatch):
    cfg = poller_mod.DeployConfig(tmp_path / "config.yaml")
    sched = poller_mod.Scheduler(cfg, bus, max_concurrent=1)
    poller = poller_mod.Poller(cfg, sched, bus, interval=10)
    await poller.tick()
    # With an empty config, tick() short-circuits before publishing — that's fine.
    types = [e["type"] for e in bus.recent_events()]
    assert types == []

    # Now seed a managed repo with no .git dir; tick should publish poll.started/finished
    sched._config._repos["fake"] = FakeRepoConfig(  # type: ignore[attr-defined]
        type="shell_only", path=str(tmp_path / "nope"), branch="main",
        url="git@github.com:example/fake.git",
    )
    await poller.tick()
    types = [e["type"] for e in bus.recent_events()]
    assert "poll.started" in types
    assert "poll.finished" in types


@pytest.mark.asyncio
async def test_tick_triggers_when_upstream_ahead(poller_mod, bus, tmp_path, monkeypatch):
    cfg = poller_mod.DeployConfig(tmp_path / "config.yaml")
    sched = poller_mod.Scheduler(cfg, bus, max_concurrent=1)
    enqueued: list[str] = []

    async def fake_enqueue(name, after, delivery):
        enqueued.append((name, after, delivery))

    monkeypatch.setattr(sched, "enqueue", fake_enqueue)

    repo_path = tmp_path / "fake-repo"
    repo_path.mkdir()
    (repo_path / ".git").mkdir()
    sched._config._repos["fake"] = FakeRepoConfig(  # type: ignore[attr-defined]
        type="shell_only",
        path=str(repo_path),
        branch="main",
        url="git@github.com:example/fake.git",
    )

    async def fake_fetch(path):
        return 0

    async def fake_local_sha(path):
        return "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"

    async def fake_upstream_sha(path, branch):
        return "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"

    # Wrap the module's sync helpers with async shims for the test:
    async def fetch_shim(path):
        return 0

    monkeypatch.setattr(poller_mod, "_fetch_origin", fetch_shim)

    import poller as pm
    monkeypatch.setattr(pm, "_local_sha", lambda path: "a" * 40)
    monkeypatch.setattr(pm, "_upstream_sha", lambda path, branch: "b" * 40)

    poller = poller_mod.Poller(sched._config, sched, bus, interval=10)  # type: ignore[arg-defined]
    await poller.tick()

    assert enqueued == [("fake", "b" * 12, "poll")]
    types = [e["type"] for e in bus.recent_events()]
    assert "poll.upstream_ahead" in types


@pytest.mark.asyncio
async def test_tick_does_not_trigger_when_up_to_date(poller_mod, bus, tmp_path, monkeypatch):
    cfg = poller_mod.DeployConfig(tmp_path / "config.yaml")
    sched = poller_mod.Scheduler(cfg, bus, max_concurrent=1)
    enqueued: list[str] = []

    async def fake_enqueue(name, after, delivery):
        enqueued.append((name, after, delivery))

    monkeypatch.setattr(sched, "enqueue", fake_enqueue)

    repo_path = tmp_path / "fake-repo"
    repo_path.mkdir()
    (repo_path / ".git").mkdir()
    sched._config._repos["fake"] = FakeRepoConfig(  # type: ignore[attr-defined]
        type="shell_only",
        path=str(repo_path),
        branch="main",
        url="git@github.com:example/fake.git",
    )

    async def fetch_shim(path):
        return 0

    import poller as pm
    monkeypatch.setattr(pm, "_fetch_origin", fetch_shim)
    monkeypatch.setattr(pm, "_local_sha", lambda path: "a" * 40)
    monkeypatch.setattr(pm, "_upstream_sha", lambda path, branch: "a" * 40)

    poller = poller_mod.Poller(sched._config, sched, bus, interval=10)  # type: ignore[arg-defined]
    await poller.tick()

    assert enqueued == []
    types = [e["type"] for e in bus.recent_events()]
    assert "poll.upstream_ahead" not in types


@pytest.mark.asyncio
async def test_poll_loop_starts_and_stops(poller_mod, bus, tmp_path, monkeypatch):
    cfg = poller_mod.DeployConfig(tmp_path / "config.yaml")
    sched = poller_mod.Scheduler(cfg, bus, max_concurrent=1)
    ticks = {"n": 0}

    async def fake_tick():
        ticks["n"] += 1
        await asyncio.sleep(0)

    poller = poller_mod.Poller(cfg, sched, bus, interval=0.05)
    monkeypatch.setattr(poller, "tick", fake_tick)
    await poller.start()
    await asyncio.sleep(0.2)
    await poller.stop()
    assert ticks["n"] >= 1
