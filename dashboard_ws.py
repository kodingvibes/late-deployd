"""
WS + REST endpoints for the /dashboard microfrontend.

Public surface:
- GET  /api/dashboard/state          — full snapshot (same shape as snapshot())
- GET  /api/dashboard/history        — time-range samples for one metric
- WS   /api/dashboard/ws              — push of {state, history} every
                                         BROADCAST_INTERVAL_S, with a one-shot
                                         backfill of the 1h range on connect.

Auth: every endpoint validates the session against
late-auth and requires global_role == 'super_admin'. The
WS upgrade carries the bearer as a `?token=` query
parameter because browsers can't set custom headers on
WebSocket requests.
"""
from __future__ import annotations

import asyncio
import json
import time
from typing import Any

from fastapi import HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse

import dashboard_history
import dashboard_state


# ---------------------------------------------------------------------------
# REST: snapshot
# ---------------------------------------------------------------------------
async def _auth_or_redirect(request: Request):
    """Same helper as dashboard_state, scoped to the request."""
    return await dashboard_state.require_super_admin_response(request)


async def api_dashboard_state(request: Request) -> JSONResponse:
    await _auth_or_redirect(request)
    snap = await dashboard_state.snapshot()
    return JSONResponse(snap)


# ---------------------------------------------------------------------------
# REST: history
# ---------------------------------------------------------------------------
async def api_dashboard_history(
    request: Request,
    metric: str = Query("cpu"),
    range: str = Query("1h"),
) -> JSONResponse:
    await _auth_or_redirect(request)
    range_seconds = dashboard_history.RANGES.get(range)
    if range_seconds is None:
        raise HTTPException(400, f"unknown range: {range}")
    if metric not in ("cpu", "memory", "swap", "load_1m", "listeners", "latency_ms"):
        raise HTTPException(400, f"unknown metric: {metric}")
    samples = await dashboard_state.history(metric, range_seconds)
    return JSONResponse({
        "metric": metric,
        "range": range,
        "range_seconds": range_seconds,
        "samples": samples,
        "fetched_at": time.time(),
    })


# ---------------------------------------------------------------------------
# WS: live feed
# ---------------------------------------------------------------------------
class _Hub:
    """Tracks active WS clients; the broadcast loop pushes to all."""

    def __init__(self) -> None:
        self.clients: set[WebSocket] = set()
        self.last_broadcast: float = 0.0
        self.lock = asyncio.Lock()

    async def add(self, ws: WebSocket) -> None:
        async with self.lock:
            self.clients.add(ws)

    async def remove(self, ws: WebSocket) -> None:
        async with self.lock:
            self.clients.discard(ws)

    async def broadcast(self, payload: dict) -> None:
        # Send-and-prune, but per-client with a hard timeout so a hung
        # socket can't stall the next tick. Sequentially awaiting
        # `send_json` on every client used to add up to seconds per
        # tick when a browser tab was throttled; with one slow client
        # it now caps at SEND_TIMEOUT_S (default 1s) and the rest of
        # the loop continues.
        async with self.lock:
            clients = list(self.clients)
        if not clients:
            return

        async def _send(ws) -> None:
            await asyncio.wait_for(ws.send_json(payload), timeout=SEND_TIMEOUT_S)

        results = await asyncio.gather(
            *[_send(ws) for ws in clients], return_exceptions=True
        )
        stale = [ws for ws, r in zip(clients, results) if isinstance(r, BaseException)]
        if stale:
            async with self.lock:
                for ws in stale:
                    self.clients.discard(ws)


HUB = _Hub()

# ponytail: per-client send timeout. A browser tab in the background
# or a dying TCP connection can hold send_json() for seconds; if we
# awaited the slow client serially, every other client would lag
# too. 1 s caps the worst case; raise if dashboards get bigger
# payloads.
SEND_TIMEOUT_S: float = 1.0


async def _broadcast_loop() -> None:
    """Background task: snapshot every BROADCAST_INTERVAL_S,
    push to every connected WS client. Appends to history
    as a side-effect (snapshot() does that)."""
    while True:
        try:
            if not HUB.clients:
                await asyncio.sleep(dashboard_state.BROADCAST_INTERVAL_S)
                continue
            snap = await dashboard_state.snapshot()
            # Small 1h history backfill on every tick so the chart can
            # render without an extra /api/dashboard/history call on
            # connect. Sequential awaits (not gather) so the roll()
            # throttle inside history() never runs from two threads at once.
            hist_1h = {
                "cpu": await dashboard_state.history("cpu", 3600),
                "memory": await dashboard_state.history("memory", 3600),
                "swap": await dashboard_state.history("swap", 3600),
                "load_1m": await dashboard_state.history("load_1m", 3600),
            }
            payload = {
                "type": "state",
                "t": time.time(),
                "state": snap,
                "history": {"1h": hist_1h},
            }
            HUB.last_broadcast = time.time()
            await HUB.broadcast(payload)
        except asyncio.CancelledError:
            return
        except Exception:
            # ponytail: never let a single bad tick kill
            # the loop. Sleep and continue.
            pass
        await asyncio.sleep(dashboard_state.BROADCAST_INTERVAL_S)


async def _broadcast_fast_loop() -> None:
    """Per-second tick for the cpu/mem/swap gauges.

    Skips the heavy gatherers (docker ps, icecast status,
    du -sb) and only runs the cheap /proc readers. The
    payload is type='state_fast' and carries only the
    'system' slice of the state plus a short history of
    the three gauges, so the gauge digits read as a live
    number rather than a slideshow.
    """
    while True:
        try:
            if not HUB.clients:
                await asyncio.sleep(dashboard_state.BROADCAST_FAST_INTERVAL_S)
                continue
            fast = await dashboard_state.fast_snapshot()
            # Append to history at the same cadence so the
            # 1m range window has data to draw.
            sys = fast.get("system", {}) or {}
            if sys.get("cpu_pct") is not None:
                dashboard_history.append_sample("cpu", sys["cpu_pct"])
            mem = sys.get("memory", {}) or {}
            if mem.get("pct") is not None:
                dashboard_history.append_sample("memory", mem["pct"])
            swap = sys.get("swap", {}) or {}
            if swap.get("pct") is not None:
                dashboard_history.append_sample("swap", swap["pct"])
            hist_1m = {
                "cpu": await dashboard_state.history("cpu", 60),
                "memory": await dashboard_state.history("memory", 60),
                "swap": await dashboard_state.history("swap", 60),
            }
            hist_5m = {
                "cpu": await dashboard_state.history("cpu", 300),
                "memory": await dashboard_state.history("memory", 300),
                "swap": await dashboard_state.history("swap", 300),
            }
            payload = {
                "type": "state_fast",
                "t": time.time(),
                "state": fast,
                "history": {"1m": hist_1m, "5m": hist_5m},
            }
            await HUB.broadcast(payload)
        except asyncio.CancelledError:
            return
        except Exception:
            pass
        await asyncio.sleep(dashboard_state.BROADCAST_FAST_INTERVAL_S)


_BROADCAST_TASK: asyncio.Task | None = None
_BROADCAST_FAST_TASK: asyncio.Task | None = None


async def api_dashboard_ws(websocket: WebSocket, token: str = Query("")) -> None:
    """Authenticated WebSocket feed.

    Browsers can't set Authorization on a WebSocket, so the
    bearer comes in as `?token=`. The endpoint opens the WS,
    then validates synchronously, then drops the connection
    if the bearer is bad.
    """
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
    await HUB.add(websocket)
    # ponytail: send initial snapshot immediately so the dashboard
    # doesn't sit empty for up to BROADCAST_INTERVAL_S (30s).
    # One targeted push per client — the broadcast loop takes over
    # for subsequent ticks.
    try:
        snap = await dashboard_state.snapshot()
        hist_1h = {
            "cpu": await dashboard_state.history("cpu", 3600),
            "memory": await dashboard_state.history("memory", 3600),
            "swap": await dashboard_state.history("swap", 3600),
            "load_1m": await dashboard_state.history("load_1m", 3600),
        }
        await websocket.send_json({
            "type": "state",
            "t": time.time(),
            "state": snap,
            "history": {"1h": hist_1h},
        })
    except Exception:
        pass
    try:
        # Keep the connection alive. We don't expect any
        # inbound messages; if the client sends one, treat
        # it as a ping and ignore.
        while True:
            try:
                msg = await asyncio.wait_for(websocket.receive_text(), timeout=60)
                if msg == "ping":
                    await websocket.send_text("pong")
            except asyncio.TimeoutError:
                # No inbound for 60s. Send a keepalive so
                # intermediate proxies don't close us.
                try:
                    await websocket.send_text("ping")
                except Exception:
                    break
    except WebSocketDisconnect:
        pass
    finally:
        await HUB.remove(websocket)


# ---------------------------------------------------------------------------
# Mount
# ---------------------------------------------------------------------------
def register(app) -> None:
    global _BROADCAST_TASK
    app.add_api_route("/api/dashboard/state", api_dashboard_state, methods=["GET"])
    app.add_api_route("/api/dashboard/history", api_dashboard_history, methods=["GET"])
    app.add_api_websocket_route("/api/dashboard/ws", api_dashboard_ws)

    @app.on_event("startup")
    async def _start_broadcast() -> None:
        global _BROADCAST_TASK, _BROADCAST_FAST_TASK
        if _BROADCAST_TASK is None or _BROADCAST_TASK.done():
            _BROADCAST_TASK = asyncio.create_task(_broadcast_loop())
        if _BROADCAST_FAST_TASK is None or _BROADCAST_FAST_TASK.done():
            _BROADCAST_FAST_TASK = asyncio.create_task(_broadcast_fast_loop())

    @app.on_event("shutdown")
    async def _stop_broadcast() -> None:
        global _BROADCAST_TASK, _BROADCAST_FAST_TASK
        for task in (_BROADCAST_TASK, _BROADCAST_FAST_TASK):
            if task and not task.done():
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass
