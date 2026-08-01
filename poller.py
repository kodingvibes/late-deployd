"""Repo poller: every N seconds, fetch origin for each managed repo and enqueue
a deploy if upstream moved. Closes the gap when GitHub webhooks are lost
(replay not available in time, transient 5xx, NAT we can't see, etc).

Why in-process and not cron: same lifecycle as the webhook receiver, same
ad-hoc deployer pool, same events. A missed tick is just the next 10-minute
window; no missed webhook can sit forever.
"""
from __future__ import annotations

import asyncio
import logging
import os
from typing import Optional

from config import DeployConfig
from events import EventBus
from scheduler import Scheduler

logger = logging.getLogger("deployd")

POLL_INTERVAL = int(os.environ.get("DEPLOYD_POLL_INTERVAL", "60"))  # 1 min default
FETCH_TIMEOUT = int(os.environ.get("DEPLOYD_POLL_FETCH_TIMEOUT", "20"))


def _local_sha(path: str) -> Optional[str]:
    import subprocess
    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=path, capture_output=True, text=True, timeout=5,
        )
        if out.returncode == 0 and out.stdout.strip():
            return out.stdout.strip()
    except Exception:
        pass
    return None


def _upstream_sha(path: str, branch: str) -> Optional[str]:
    """Return the SHA origin/<branch> currently references locally.

    Done with `git rev-parse origin/<branch>` — no second fetch needed if
    `git fetch` already ran upstream in this loop.
    """
    import subprocess
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--verify", f"origin/{branch}"],
            cwd=path, capture_output=True, text=True, timeout=5,
        )
        if out.returncode == 0 and out.stdout.strip():
            return out.stdout.strip()
    except Exception:
        pass
    return None


async def _fetch_origin(path: str) -> int:
    import asyncio
    rc_holder: dict[str, int] = {}

    def _run() -> None:
        import subprocess
        try:
            p = subprocess.run(
                ["git", "fetch", "--quiet", "origin"],
                cwd=path, capture_output=True, text=True, timeout=FETCH_TIMEOUT,
            )
            rc_holder["rc"] = p.returncode
            if p.stdout:
                rc_holder["out"] = p.stdout  # type: ignore[assignment]
            if p.stderr:
                rc_holder["err"] = p.stderr  # type: ignore[assignment]
        except subprocess.TimeoutExpired:
            rc_holder["rc"] = 124
        except Exception as e:
            rc_holder["rc"] = 1
            rc_holder["err"] = str(e)  # type: ignore[assignment]

    await asyncio.to_thread(_run)
    return rc_holder.get("rc", 1)


class Poller:
    @property
    def interval(self) -> int:
        return self._interval

    def __init__(
        self,
        config: DeployConfig,
        scheduler: Scheduler,
        events: EventBus,
        interval: int = POLL_INTERVAL,
    ) -> None:
        self._config = config
        self._scheduler = scheduler
        self._events = events
        self._interval = interval
        self._task: Optional[asyncio.Task] = None
        self._stopping = asyncio.Event()

    async def start(self) -> None:
        if self._task is not None:
            return
        self._stopping.clear()
        self._task = asyncio.create_task(self._loop(), name="deployd-poller")

    async def stop(self) -> None:
        self._stopping.set()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None

    async def _loop(self) -> None:
        # First tick happens immediately so a freshly-started daemon catches
        # whatever was missed while we were down. Webhooks already in flight
        # still win — the scheduler's per-repo lock serializes them with us.
        while not self._stopping.is_set():
            try:
                await self.tick()
            except Exception as e:
                logger.warning("poller tick failed: %s", e)
            try:
                await asyncio.wait_for(self._stopping.wait(), timeout=self._interval)
            except asyncio.TimeoutError:
                pass

    async def tick(self) -> None:
        self._config.reload_if_changed()
        repos = list(self._config.repos.values())
        if not repos:
            return
        self._events.publish("poll.started", payload={"interval": self._interval, "repos": len(repos)})
        checked = 0
        triggered = 0
        for cfg in repos:
            self._config.reload_if_changed()
            cfg = self._config.get(cfg.name) or cfg
            if not cfg.url:
                continue
            if not os.path.isdir(os.path.join(cfg.path, ".git")):
                continue
            local = _local_sha(cfg.path)
            if not local:
                continue
            rc = await _fetch_origin(cfg.path)
            checked += 1
            if rc != 0:
                self._events.publish(
                    "poll.fetch_failed",
                    repo=cfg.name,
                    payload={"rc": rc},
                )
                continue
            upstream = _upstream_sha(cfg.path, cfg.branch)
            if not upstream or upstream == local:
                continue
            triggered += 1
            self._events.publish(
                "poll.upstream_ahead",
                repo=cfg.name,
                payload={"local": local[:12], "upstream": upstream[:12]},
            )
            await self._scheduler.enqueue(cfg.name, upstream[:12], "poll")
        self._events.publish("poll.finished", payload={"checked": checked, "triggered": triggered})
