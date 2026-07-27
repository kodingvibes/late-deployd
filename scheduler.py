from __future__ import annotations

import asyncio
import logging
import asyncio
import logging

from config import DeployConfig, RepoConfig
from deployers import run_deploy_sync
from events import EventBus

logger = logging.getLogger("deployd")


class Scheduler:
    def __init__(self, config: DeployConfig, events: EventBus, max_concurrent: int = 2):
        self._queue: asyncio.Queue = asyncio.Queue()
        self._sem = asyncio.Semaphore(max_concurrent)
        self._repo_locks: dict[str, asyncio.Lock] = {}
        self._www_lock = asyncio.Lock()
        self._config = config
        self._events = events
        self._workers: list[asyncio.Task] = []

    async def start(self, n: int = 2) -> None:
        self._workers = [asyncio.create_task(self._worker()) for _ in range(n)]

    async def stop(self) -> None:
        for w in self._workers:
            w.cancel()

    def get_www_lock(self) -> asyncio.Lock:
        return self._www_lock

    async def enqueue(self, repo_name: str, after: str, delivery: str) -> None:
        config = self._config.get(repo_name)
        if not config:
            return
        await self._queue.put((repo_name, config, after, delivery))
        self._events.publish("deploy.queued", repo=repo_name, delivery=delivery, payload={"after": after})

    def _touches_www(self, config: RepoConfig) -> bool:
        if config.type == "shell_only":
            return True
        if config.type == "micro" and getattr(config, "rebuild_shell", False):
            return True
        return False

    async def _worker(self) -> None:
        while True:
            repo_name, config, after, delivery = await self._queue.get()
            lock = self._repo_locks.setdefault(repo_name, asyncio.Lock())
            async with lock:
                async with self._sem:
                    if self._touches_www(config):
                        async with self._www_lock:
                            await self._run_deploy(repo_name, config, after, delivery)
                    else:
                        await self._run_deploy(repo_name, config, after, delivery)

    async def _run_deploy(self, repo_name: str, config: RepoConfig, after: str, delivery: str) -> None:
        self._events.publish("deploy.started", repo=repo_name, delivery=delivery, payload={"after": after})
        log: list[str] = []

        def step(line: str) -> None:
            log.append(line)
            self._events.publish("deploy.step", repo=repo_name, delivery=delivery, payload={"line": line, "log": log[-20:]})

        try:
            rc = await asyncio.to_thread(run_deploy_sync, repo_name, config, step)
        except Exception as e:
            step(f"[error] {type(e).__name__}: {e}")
            rc = 1

        if rc == 0:
            self._events.publish("deploy.success", repo=repo_name, delivery=delivery, payload={"after": after, "log": log})
        else:
            self._events.publish("deploy.failure", repo=repo_name, delivery=delivery, payload={"after": after, "log": log, "rc": rc})