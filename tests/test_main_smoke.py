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
