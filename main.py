#!/usr/bin/env python3
"""
late-deployd: GitHub webhook receiver that auto-deploys repos on push to main.

Endpoints:
  POST /deploy-webhook       -> receive GitHub push events (returns 202, deploys async)
  GET  /health               -> health check
  GET  /logs                 -> list recent deploy logs
  GET  /api/deployd/events   -> recent deploy events (paginated, filterable)
  WS   /api/deployd/events/ws -> live event stream

Config: /root/.deployd/config.yaml (YAML, hot-reload on file change)
Events: /root/.deployd/events.db (SQLite, 30-day retention)
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Header, HTTPException, Query, Request, WebSocket, WebSocketDisconnect

import dashboard_history  # noqa: F401
import dashboard_state  # noqa: F401
import dashboard_ws  # noqa: F401
from config import CONFIG_PATH, DeployConfig
from events import EventBus
from poller import Poller
from scheduler import Scheduler

LOG_DIR = Path(os.environ.get("LOG_DIR", "/var/log/late-deployd"))
LOG_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("deployd")

# ---------------------------------------------------------------------------
# Globals (initialized in startup)
# ---------------------------------------------------------------------------
CONFIG = DeployConfig(CONFIG_PATH)
EVENTS = EventBus()
SCHEDULER = Scheduler(CONFIG, EVENTS, max_concurrent=2)
POLLER = Poller(CONFIG, SCHEDULER, EVENTS)

APP = FastAPI(title="late-deployd")
dashboard_ws.register(APP)


# ---------------------------------------------------------------------------
# HTTP handlers
# ---------------------------------------------------------------------------
@APP.get("/health")
async def health() -> dict:
    return {
        "ok": True,
        "time": __import__("deployers", fromlist=["now_iso"]).now_iso(),
        "repos": CONFIG.repo_names,
    }


@APP.get("/logs")
async def logs(limit: int = 10) -> list:
    files = sorted(LOG_DIR.glob("*.log"), key=lambda p: p.stat().st_mtime, reverse=True)[:limit]
    return [
        {
            "name": f.name,
            "mtime": __import__("datetime").datetime.fromtimestamp(
                f.stat().st_mtime, tz=__import__("datetime").timezone.utc
            ).isoformat(),
            "size": f.stat().st_size,
        }
        for f in files
    ]


@APP.get("/api/deployd/events")
async def get_events(
    limit: int = Query(50, ge=1, le=200),
    repo: Optional[str] = Query(None),
    type: Optional[str] = Query(None),
) -> list:
    return EVENTS.recent_events(limit=limit, repo=repo, type=type)


@APP.websocket("/api/deployd/events/ws")
async def events_ws(websocket: WebSocket, token: str = Query("")) -> None:
    if not token or not dashboard_state.LATE_AUTH_SECRET:
        await websocket.close(code=4401)
        return
    import httpx
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            r = await client.get(
                f"{dashboard_state.LATE_AUTH_URL}/api/auth/validate",
                headers={
                    "Authorization": f"Bearer {dashboard_state.LATE_AUTH_SECRET}",
                    "X-Session-Id": token,
                },
            )
    except Exception:
        await websocket.close(code=4403)
        return
    if r.status_code != 200:
        await websocket.close(code=4401)
        return
    body = r.json()
    user = body.get("user", body) if isinstance(body, dict) else body
    if user.get("global_role") != "super_admin":
        await websocket.close(code=4403)
        return

    await websocket.accept()
    q = await EVENTS.subscribe()
    try:
        while True:
            try:
                event = await asyncio.wait_for(q.get(), timeout=60)
                await websocket.send_json(event)
            except asyncio.TimeoutError:
                try:
                    await websocket.send_text("ping")
                except Exception:
                    break
    except WebSocketDisconnect:
        pass
    finally:
        EVENTS.unsubscribe(q)


@APP.post("/deploy-webhook")
async def deploy_webhook(
    request: Request,
    background: bool = True,
    x_hub_signature_256: Optional[str] = Header(default=None),
    x_github_event: Optional[str] = Header(default=None),
    x_github_delivery: Optional[str] = Header(default=None),
) -> dict:
    from deployers import verify_signature

    body = await request.body()

    if not verify_signature(body, x_hub_signature_256):
        logger.warning("invalid or missing signature; delivery=%s", x_github_delivery)
        EVENTS.publish("webhook.invalid_signature", delivery=x_github_delivery or "")
        raise HTTPException(status_code=401, detail="invalid signature")

    if x_github_event != "push":
        logger.info("ignored event: %s", x_github_event)
        EVENTS.publish("webhook.ignored", delivery=x_github_delivery or "", payload={"event": x_github_event})
        return {"ok": True, "ignored": True, "event": x_github_event}

    payload = json.loads(body.decode("utf-8"))
    repo_full = payload.get("repository", {}).get("full_name", "")
    ref = payload.get("ref", "")
    after = payload.get("after", "")[:12]

    CONFIG.reload_if_changed()

    repo_name = repo_full.split("/")[-1]
    config = CONFIG.get(repo_name)
    if not config:
        logger.info("repo not managed: %s", repo_full)
        EVENTS.publish("webhook.ignored", delivery=x_github_delivery or "", payload={"repo": repo_full, "reason": "not managed"})
        return {"ok": True, "ignored": True, "repo": repo_full}

    expected_ref = f"refs/heads/{config.branch}"
    if ref != expected_ref:
        logger.info("ignored ref for %s: %s", repo_name, ref)
        EVENTS.publish("webhook.ignored", repo=repo_name, delivery=x_github_delivery or "", payload={"ref": ref})
        return {"ok": True, "ignored": True, "ref": ref}

    logger.info("accepted deploy %s @ %s (%s)", repo_name, after, x_github_delivery)
    EVENTS.publish("webhook.received", repo=repo_name, delivery=x_github_delivery or "", payload={"after": after, "ref": ref})

    await SCHEDULER.enqueue(repo_name, after, x_github_delivery or "")

    return {
        "ok": True,
        "accepted": True,
        "repo": repo_name,
        "ref": ref,
        "after": after,
    }


# ---------------------------------------------------------------------------
# Startup / shutdown
# ---------------------------------------------------------------------------
@APP.on_event("startup")
async def _startup() -> None:
    EVENTS.set_loop(asyncio.get_event_loop())
    await SCHEDULER.start(n=2)
    logger.info("scheduler started with 2 workers")
    await POLLER.start()
    logger.info("poller started (interval=%ss)", POLLER.interval)


@APP.on_event("shutdown")
async def _shutdown() -> None:
    await POLLER.stop()
    await SCHEDULER.stop()
    logger.info("scheduler stopped")