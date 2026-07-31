"""Smoke test for the FastAPI app in main.py."""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import importlib
import json
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def app_client(monkeypatch, tmp_path):
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(
        "repos:\n"
        "  late.kodingvibes.com:\n"
        "    path: /tmp/shellx\n"
        "    branch: main\n"
        "    type: shell_only\n"
        "  microradio:\n"
        "    path: /tmp/microradio\n"
        "    branch: main\n"
        "    type: micro\n"
        "    micro_name: radio\n"
        "    build_script: /tmp/build.sh\n"
        "    rebuild_shell: true\n"
    )
    monkeypatch.setenv("DEPOYD_CONFIG", str(cfg_path))
    monkeypatch.setenv("DEPOYD_DB_PATH", str(tmp_path / "events.db"))
    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", "smoke-secret")
    monkeypatch.setenv("LOG_DIR", str(tmp_path / "logs"))
    monkeypatch.setenv("LATE_DASHBOARD_HISTORY_DIR", str(tmp_path / "metrics"))
    monkeypatch.setenv("LATE_AUTH_SECRET", "")
    (tmp_path / "logs").mkdir(parents=True, exist_ok=True)
    (tmp_path / "metrics").mkdir(parents=True, exist_ok=True)
    for m in list(sys.modules.keys()):
        if m.startswith(("config", "events", "deployers", "scheduler", "dashboard", "main")):
            sys.modules.pop(m, None)
    main = importlib.import_module("main")
    with TestClient(main.APP) as client:
        yield client, main


def make_sig(secret: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def test_health_returns_ok_and_repo_names(app_client):
    client, _ = app_client
    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert set(body["repos"]) == {"late.kodingvibes.com", "microradio"}
    assert "poll_interval" in body


def test_health_poll_one_triggers_tick(app_client, monkeypatch):
    client, main = app_client
    called = {"n": 0}

    async def fake_tick():
        called["n"] += 1

    monkeypatch.setattr(main.POLLER, "tick", fake_tick)
    r = client.get("/health?poll=1")
    assert r.status_code == 200
    body = r.json()
    assert body["poll_triggered"] is True
    # asyncio.create_task runs the coroutine on the loop; the test client uses
    # the app's loop so the task completes before the response returns. Wait
    # a beat just in case.
    import time
    for _ in range(10):
        if called["n"]:
            break
        time.sleep(0.05)
    assert called["n"] == 1


def test_health_without_poll_does_not_trigger(app_client, monkeypatch):
    client, main = app_client
    called = {"n": 0}

    async def fake_tick():
        called["n"] += 1

    monkeypatch.setattr(main.POLLER, "tick", fake_tick)
    client.get("/health")
    client.get("/health?poll=0")
    client.get("/health?poll=true")  # only the literal "1" triggers
    assert called["n"] == 0


def test_health_reports_repos_status_and_in_flight(app_client):
    client, main = app_client
    main.EVENTS.publish("deploy.success", repo="late.kodingvibes.com", delivery="d-1", payload={"after": "abc"})
    main.EVENTS.publish("deploy.started", repo="microradio", delivery="d-2", payload={"after": "def"})

    r = client.get("/health")
    body = r.json()
    assert body["ok"] is True
    assert body["repos_status"]["late.kodingvibes.com"]["state"] == "deploy.success"
    assert body["repos_status"]["microradio"]["state"] == "deploy.started"
    assert body["in_flight"] == ["microradio"]


def test_health_flags_stuck_started(app_client, monkeypatch):
    client, main = app_client
    main.EVENTS.publish("deploy.started", repo="late.kodingvibes.com", delivery="d-old", payload={"after": "x"})
    # backdate the event so age > 20min
    import sqlite3
    with sqlite3.connect(main.EVENTS._db_path) as conn:  # type: ignore[attr-defined]
        conn.execute("UPDATE events SET timestamp = ? WHERE delivery = ?", (0.0, "d-old"))
        conn.commit()

    body = client.get("/health").json()
    assert body["ok"] is False
    assert "degraded" in body
    assert "late.kodingvibes.com" in body["degraded"]


def test_health_reports_last_failure_and_success_age(app_client):
    client, main = app_client
    main.EVENTS.publish("deploy.failure", repo="microradio", delivery="d-f", payload={"after": "x", "rc": 1})
    body = client.get("/health").json()
    assert body["last_failure_age_seconds"] is not None
    assert body["last_failure_age_seconds"] >= 0


def test_logs_lists_only_recent(app_client):
    client, _ = app_client
    r = client.get("/logs")
    assert r.status_code == 200
    assert isinstance(r.json(), list)


def test_recent_events_endpoint(app_client):
    client, _ = app_client
    r = client.get("/api/deployd/events")
    assert r.status_code == 200
    assert isinstance(r.json(), list)


def test_recent_events_pagination_and_filters(app_client):
    client, _ = app_client
    client.get("/api/deployd/events?limit=5&type=foo")
    r = client.get("/api/deployd/events?limit=1&repo=none")
    assert r.status_code == 200
    assert isinstance(r.json(), list)


def test_webhook_rejects_invalid_signature(app_client):
    client, _ = app_client
    r = client.post(
        "/deploy-webhook",
        content=b'{"ref":"refs/heads/main"}',
        headers={
            "x-hub-signature-256": "sha256=00",
            "x-github-event": "push",
            "x-github-delivery": "d-bad",
        },
    )
    assert r.status_code == 401


def test_webhook_rejects_missing_signature(app_client):
    client, _ = app_client
    r = client.post(
        "/deploy-webhook",
        content=b"{}",
        headers={"x-github-event": "push", "x-github-delivery": "d-miss"},
    )
    assert r.status_code == 401


def test_webhook_ignores_non_push_event(app_client):
    client, _ = app_client
    body = b'{"ref":"refs/heads/main","after":"abc123456","repository":{"full_name":"kodingvibes/late.kodingvibes.com"}}'
    sig = make_sig("smoke-secret", body)
    r = client.post(
        "/deploy-webhook",
        content=body,
        headers={
            "x-hub-signature-256": sig,
            "x-github-event": "pull_request",
            "x-github-delivery": "d-ign",
        },
    )
    assert r.status_code == 200
    assert r.json().get("ignored") is True


def test_webhook_ignores_unmanaged_repo(app_client):
    client, _ = app_client
    body = json.dumps({
        "ref": "refs/heads/main",
        "after": "abc123456",
        "repository": {"full_name": "someone/else"},
    }).encode()
    sig = make_sig("smoke-secret", body)
    r = client.post(
        "/deploy-webhook",
        content=body,
        headers={
            "x-hub-signature-256": sig,
            "x-github-event": "push",
            "x-github-delivery": "d-um",
        },
    )
    assert r.status_code == 200
    assert r.json().get("ignored") is True


def test_webhook_ignores_wrong_branch(app_client):
    client, _ = app_client
    body = json.dumps({
        "ref": "refs/heads/dev",
        "after": "abc123456",
        "repository": {"full_name": "kodingvibes/late.kodingvibes.com"},
    }).encode()
    sig = make_sig("smoke-secret", body)
    r = client.post(
        "/deploy-webhook",
        content=body,
        headers={
            "x-hub-signature-256": sig,
            "x-github-event": "push",
            "x-github-delivery": "d-wb",
        },
    )
    assert r.status_code == 200
    body_json = r.json()
    assert body_json.get("ignored") is True
    assert body_json.get("ref") == "refs/heads/dev"


def test_webhook_enqueues_valid_push(monkeypatch, app_client):
    client, main = app_client
    monkeypatch.setattr(main.SCHEDULER, "enqueue", lambda *a, **kw: _async_noop())
    body = json.dumps({
        "ref": "refs/heads/main",
        "after": "abc123456",
        "repository": {"full_name": "kodingvibes/late.kodingvibes.com"},
    }).encode()
    sig = make_sig("smoke-secret", body)
    r = client.post(
        "/deploy-webhook",
        content=body,
        headers={
            "x-hub-signature-256": sig,
            "x-github-event": "push",
            "x-github-delivery": "d-ok",
        },
    )
    assert r.status_code == 200
    assert r.json().get("accepted") is True
    assert r.json().get("repo") == "late.kodingvibes.com"


def _async_noop():
    import asyncio
    return asyncio.sleep(0)
