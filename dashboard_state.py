"""
State + history backend for the /dashboard microfrontend.

Splits the previous inline state-gathering out of the
HTML-render path so it can serve the WS feed the new MF
expects, and adds a periodic background task that pushes
fresh snapshots to every connected client.

Public surface (mounted by dashboard_ws.py):
- snapshot() -> dict
- history(metric, range_seconds) -> list[dict]
- start() / stop() for the background gather loop.
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import sqlite3
import subprocess
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional
from zoneinfo import ZoneInfo

import httpx

import dashboard_history

# Backend service URLs — local loopback, the deployd runs
# next to them.
LATE_AUTH_URL = os.environ.get("LATE_AUTH_URL", "http://127.0.0.1:9300")
CHAT_URL = "http://127.0.0.1:9100"
ICECAST_URL = "http://127.0.0.1:8000"
LATE_AUTH_SECRET = os.environ.get("LATE_AUTH_SECRET", "")

# Time-series store path. Override for tests.
os.environ.setdefault("LATE_DASHBOARD_HISTORY_DIR", "/var/lib/late-dashboard/metrics")
HISTORY_DIR = Path(os.environ["LATE_DASHBOARD_HISTORY_DIR"])

DB_AUTH = os.environ.get("LATE_AUTH_DB", "/data/late-auth/auth.db")
DB_CHAT = os.environ.get("LATE_CHAT_DB", "/data/late-chat-service/chat.db")

# ponytail: 4 s ceiling per gather. The slowest thing in
# practice is /status-json.xsl, which returns in ~50 ms.
# 4 s is enough to absorb a slow healthcheck and short
# enough to not stall the WS broadcast loop.
GATHER_TIMEOUT_S = 4.0

# How often the background loop snapshots and broadcasts.
BROADCAST_INTERVAL_S = 2.0

# ponytail: gauges (cpu/mem/swap) get their own faster tick
# because a 3s interval makes the tachometer digits read as
# a slideshow. The full /api/dashboard/state still broadcasts
# at 3s so the heavy gatherers (docker ps, icecast status,
# du -sb) don't run every second.
BROADCAST_FAST_INTERVAL_S = 5.0

# 18 SomaFM streams — exposed in the state so the MF can
# render the catalog. Kept in sync with start_soma_relays.sh.
STREAMS = [
    ("groovesalad", "Groove Salad"),
    ("dronezone", "Drone Zone"),
    ("fluid", "Fluid"),
    ("indiepop", "Indie Pop Rocks!"),
    ("u80s", "Underground 80s"),
    ("vaporwaves", "Vapor Waves"),
    ("metal", "Metal Detector"),
    ("dubstep", "Dub Step Beyond"),
    ("7soul", "Seven Inch Soul"),
    ("beatblender", "Beat Blender"),
    ("bootliquor", "Boot Liquor"),
    ("doomed", "Doomed"),
    ("illstreet", "Illinois Street Lounge"),
    ("lush", "Lush"),
    ("poptron", "PopTron"),
    ("secretagent", "Secret Agent"),
    ("suburbsofgoa", "Suburbs of Goa"),
    ("thetrip", "The Trip"),
]


# ---------------------------------------------------------------------------
# Auth gate (lifted from dashboard.py so the WS endpoint
# can use the same redirect-on-no-auth helper as the HTML
# page used to).
# ---------------------------------------------------------------------------
async def require_super_admin_response(request) -> Any:
    """Validate session and require super_admin. Returns either
    the user dict or an HTTPException. JSON clients (REST,
    WS) get 401/403; the WS endpoint translates that into
    a close code. No redirects — there is no HTML page
    to redirect to anymore.
    """
    from fastapi import HTTPException
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(401, "missing bearer token")
    token = auth[7:].strip()
    if not token:
        raise HTTPException(401, "empty bearer token")
    if not LATE_AUTH_SECRET:
        raise HTTPException(503, "LATE_AUTH_SECRET not configured on deployd")
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            r = await client.get(
                f"{LATE_AUTH_URL}/api/auth/validate",
                headers={
                    "Authorization": f"Bearer {LATE_AUTH_SECRET}",
                    "X-Session-Id": token,
                },
            )
    except httpx.HTTPError as e:
        raise HTTPException(503, f"late-auth unreachable: {e}")
    if r.status_code != 200:
        raise HTTPException(401, "invalid session")
    body = r.json()
    user = body.get("user", body) if isinstance(body, dict) else body
    if user.get("global_role") != "super_admin":
        raise HTTPException(403, "super_admin required")
    return user


# ---------------------------------------------------------------------------
# Gatherers (each returns a small dict; missing data is fine).
# ---------------------------------------------------------------------------
async def check_http(url: str, *, timeout: float = 2.0) -> dict:
    t0 = time.perf_counter()
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            r = await client.get(url)
        ms = int((time.perf_counter() - t0) * 1000)
        return {"ok": r.status_code < 500, "status": r.status_code, "ms": ms}
    except httpx.HTTPError as e:
        ms = int((time.perf_counter() - t0) * 1000)
        return {"ok": False, "status": 0, "ms": ms, "error": str(e)}


def _loadavg() -> Optional[str]:
    try:
        with open("/proc/loadavg") as f:
            return " ".join(f.read().split()[:3])
    except OSError:
        return None


def _mem() -> dict:
    info: dict[str, int] = {}
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if ":" in line:
                    k, v = line.split(":", 1)
                    v = v.strip().split()[0] if v.strip() else "0"
                    info[k] = int(v) * 1024
    except OSError:
        pass
    total = info.get("MemTotal", 0)
    avail = info.get("MemAvailable", 0)
    used = max(total - avail, 0)
    pct = int(used * 100 / total) if total else 0
    return {"total": total, "used": used, "avail": avail, "pct": pct}


def _swap() -> dict:
    info: dict[str, int] = {}
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if ":" in line:
                    k, v = line.split(":", 1)
                    v = v.strip().split()[0] if v.strip() else "0"
                    info[k] = int(v) * 1024
    except OSError:
        pass
    total = info.get("SwapTotal", 0)
    free = info.get("SwapFree", 0)
    used = max(total - free, 0)
    pct = int(used * 100 / total) if total else 0
    return {"total": total, "used": used, "free": free, "pct": pct}


def _disk(path: str) -> dict:
    # shutil.disk_usage covers bytes but not inodes; statvfs adds
    # f_files/f_ffree. The /data filesystem is typically a single
    # ext4 mount on a small droplet, so this is cheap.
    try:
        st = shutil.disk_usage(path)
    except OSError as e:
        return {"error": str(e)}
    pct = int(st.used * 100 / st.total) if st.total else 0
    out: dict = {"total": st.total, "used": st.used, "free": st.free, "pct": pct}
    try:
        sv = os.statvfs(path)
        inodes_total = sv.f_files
        inodes_free = sv.f_ffree
        inodes_used = inodes_total - inodes_free
        inodes_pct = int(inodes_used * 100 / inodes_total) if inodes_total else 0
        out["inodes"] = {
            "total": inodes_total,
            "used": inodes_used,
            "free": inodes_free,
            "pct": inodes_pct,
        }
    except OSError:
        pass
    return out


def _top_dirs() -> list[dict]:
    # A focused top-5 of the directories that actually grow on this
    # host. `du -sb` is blocking and can take a while once these
    # directories are large, so this only ever runs from the
    # expensive (3s) path, never from fast_snapshot's per-second tick.
    candidates = [
        ("/data/late-auth", "/data/late-auth"),
        ("/data/late-chat-service", "/data/late-chat-service"),
        ("/data/chat-bridge", "/data/chat-bridge"),
        ("/var/log/late-deployd", "/var/log/late-deployd"),
        ("/var/lib/late-dashboard", "/var/lib/late-dashboard"),
    ]
    rows: list[dict] = []
    for path, label in candidates:
        try:
            out = subprocess.run(
                ["du", "-sb", "--", path],
                capture_output=True, text=True, timeout=4,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired):
            continue
        if out.returncode != 0 or not out.stdout.strip():
            continue
        try:
            size = int(out.stdout.split()[0])
        except (ValueError, IndexError):
            continue
        rows.append({"path": path, "label": label, "bytes": size})
    rows.sort(key=lambda r: r["bytes"], reverse=True)
    return rows[:5]


def _uptime() -> Optional[int]:
    try:
        with open("/proc/uptime") as f:
            return int(float(f.read().split()[0]))
    except (OSError, ValueError, IndexError):
        return None


def _cpu_sample() -> Optional[tuple]:
    try:
        with open("/proc/stat") as f:
            line = f.readline()
        parts = line.split()
        nums = [int(x) for x in parts[1:]]
        total = sum(nums)
        idle = nums[3] if len(nums) > 3 else 0
        return total - idle, total
    except (OSError, ValueError, IndexError):
        return None


_prev_cpu: tuple | None = None

def _cpu_pct() -> Optional[int]:
    global _prev_cpu
    b = _cpu_sample()
    if b is None:
        return None
    if _prev_cpu is None:
        _prev_cpu = b
        return 0
    busy = b[0] - _prev_cpu[0]
    total = b[1] - _prev_cpu[1]
    _prev_cpu = b
    if total <= 0:
        return 0
    return int(busy * 100 / total)


def _system_cheap_sync() -> dict:
    """loadavg/mem/swap/cpu: what the per-second gauge tick needs.
    Still blocking (the cpu sample sleeps 200ms) — only ever call
    this via asyncio.to_thread, never directly from a coroutine."""
    return {
        "loadavg": _loadavg(),
        "memory": _mem(),
        "swap": _swap(),
        "cpu_pct": _cpu_pct(),
    }


def _system_extras_sync() -> dict:
    """disk/uptime: only needed by the full snapshot.
    top_dirs (du -sb) removed — was spawning 5 subprocesses per tick."""
    return {
        "disk_data": _disk("/data"),
        "uptime_s": _uptime(),
    }


async def _system_cheap() -> dict:
    return await asyncio.to_thread(_system_cheap_sync)


async def g_system() -> dict:
    cheap, extras = await asyncio.gather(
        asyncio.to_thread(_system_cheap_sync),
        asyncio.to_thread(_system_extras_sync),
    )
    return {**cheap, **extras}


async def g_service_health() -> dict:
    auth, chat, deployd = await asyncio.gather(
        check_http(f"{LATE_AUTH_URL}/api/auth/healthz"),
        check_http(f"{CHAT_URL}/healthz"),
        _self_probe(),
    )
    return {
        "late_auth_service": auth,
        "late_chat_service": chat,
        "late_deployd": deployd,
    }


async def _self_probe() -> dict:
    t0 = time.perf_counter()
    await asyncio.sleep(0)
    return {"ok": True, "status": 200, "ms": int((time.perf_counter() - t0) * 1000)}


async def g_docker() -> dict:
    def _ps() -> list[dict]:
        try:
            out = subprocess.run(
                ["docker", "ps", "--no-trunc", "--format", "{{.ID}}|{{.Names}}|{{.Image}}|{{.Status}}|{{.Ports}}"],
                capture_output=True, text=True, timeout=3,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return []
        if out.returncode != 0:
            return []
        rows = []
        for line in out.stdout.strip().splitlines():
            parts = line.split("|", 4)
            if len(parts) != 5:
                continue
            cid, name, image, status, ports = parts
            rows.append({
                "id": cid[:12],
                "name": name,
                "image": image,
                "status": status,
                "ports": ports,
            })
        return rows

    def _collect() -> dict:
        return {"containers": _ps()}

    return await asyncio.to_thread(_collect)


# ---------------------------------------------------------------------------
# Snapshot + history (read paths exposed to the WS / REST).
# ---------------------------------------------------------------------------
async def g_icecast() -> dict:
    """Gather icecast status from /status-json.xsl."""
    import httpx
    try:
        async with httpx.AsyncClient(timeout=2.0) as client:
            r = await client.get(f"{ICECAST_URL}/status-json.xsl")
        if r.status_code != 200:
            return {"ok": False, "sources": [], "total_listeners": 0}
        data = r.json()
        ic = data.get("icestats", {}) or {}
        sources_raw = ic.get("source", []) or []
        if isinstance(sources_raw, dict):
            sources_raw = [sources_raw]
        sources = []
        total = 0
        for s in sources_raw:
            if not isinstance(s, dict):
                continue
            mount = (s.get("server_name", "") or "").strip()
            if not mount:
                mount = (s.get("listenurl", "") or "").rsplit("/", 1)[-1]
            listeners = int(s.get("listeners", 0) or 0)
            title = (s.get("server_description", "") or "").strip()
            sources.append({"mount": mount, "listeners": listeners, "title": title})
            total += listeners
        return {"ok": True, "sources": sources, "total_listeners": total}
    except Exception:
        return {"ok": False, "sources": [], "total_listeners": 0}


async def g_deploys() -> dict:
    """Gather latest deploy events."""
    from events import EVENTS
    try:
        latest = EVENTS.latest_deploys()
        deploys = []
        for repo, info in latest.items():
            deploys.append({
                "file": f"{repo}.log",
                "mtime": info.get("timestamp", 0),
                "ok": info.get("type") == "deploy.success",
                "commit": info.get("after", ""),
            })
        deploys.sort(key=lambda d: d["mtime"], reverse=True)
        return {"deploys": deploys}
    except Exception:
        return {"deploys": []}


async def g_db() -> dict:
    """Gather database sizes and counts."""
    def _db_info(path: str) -> dict:
        try:
            size = os.path.getsize(path)
        except OSError:
            size = 0
        counts: dict[str, int] = {}
        try:
            with sqlite3.connect(path, timeout=2) as conn:
                tables = conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
                for (tname,) in tables:
                    try:
                        (cnt,) = conn.execute(f"SELECT COUNT(*) FROM \"{tname}\"").fetchone() or (0,)
                        counts[tname] = cnt
                    except Exception:
                        pass
        except Exception:
            pass
        return {"bytes": size, "counts": counts}

    auth, chat = await asyncio.gather(
        asyncio.to_thread(lambda: _db_info(DB_AUTH)),
        asyncio.to_thread(lambda: _db_info(DB_CHAT)),
    )
    return {"auth_db": auth, "chat_db": chat}


async def g_streams() -> dict:
    """Return the static stream list."""
    return {
        "streams": [
            {"mount": m, "label": l}
            for m, l in STREAMS
        ]
    }


GATHERERS: list[tuple[str, Callable[[], Awaitable[dict]]]] = [
    ("system", g_system),
    ("services", g_service_health),
    ("docker", g_docker),
    ("icecast", g_icecast),
    ("deploys", g_deploys),
    ("db", g_db),
    ("streams", g_streams),
]


async def _one(name: str, fn) -> tuple[str, dict]:
    try:
        return name, await asyncio.wait_for(fn(), timeout=GATHER_TIMEOUT_S)
    except (asyncio.TimeoutError, Exception) as e:
        return name, {"error": f"{type(e).__name__}: {e}"}


async def fast_snapshot() -> dict:
    """Cheap snapshot for the per-second gauge tick."""
    sys = await _system_cheap()
    return {
        "system": sys,
        "gathered_at": datetime.now(ZoneInfo("UTC")).isoformat(),
    }


async def snapshot() -> dict:
    pairs = await asyncio.gather(*[_one(n, f) for n, f in GATHERERS])
    out = dict(pairs)
    out["gathered_at"] = datetime.now(ZoneInfo("UTC")).isoformat()
    system = out.get("system", {}) or {}
    if system.get("cpu_pct") is not None:
        dashboard_history.append_sample("cpu", system["cpu_pct"])
    mem = system.get("memory", {}) or {}
    if mem.get("pct") is not None:
        dashboard_history.append_sample("memory", mem["pct"])
    swap = system.get("swap", {}) or {}
    if swap.get("pct") is not None:
        dashboard_history.append_sample("swap", swap["pct"])
    loadavg = (system.get("loadavg") or "").split()
    if loadavg:
        try:
            dashboard_history.append_sample("load_1m", float(loadavg[0]))
        except ValueError:
            pass
    return out


_last_roll = 0.0
# roll() scans every metric's .jsonl file; retention is 31 days, so
# rolling more often than this is pointless work.
ROLL_INTERVAL_S = 60.0


def _history_sync(metric: str, range_seconds: int) -> list[dict]:
    global _last_roll
    now = time.time()
    if now - _last_roll > ROLL_INTERVAL_S:
        dashboard_history.roll()
        _last_roll = now
    return dashboard_history.read_samples(metric, range_seconds)


async def history(metric: str, range_seconds: int) -> list[dict]:
    """Return time-series samples for a metric in a range.

    Offloaded to a thread: roll()/read_samples() do blocking file I/O,
    and the broadcast loops in dashboard_ws.py call this several times
    per tick — running it inline was stalling the event loop.
    """
    if metric not in ("cpu", "memory", "swap", "load_1m", "listeners", "latency_ms"):
        return []
    return await asyncio.to_thread(_history_sync, metric, range_seconds)
