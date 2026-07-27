"""Unit tests for deployers (mocks subprocess; no git, no shell, no /root)."""
from __future__ import annotations

import importlib
import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest


@pytest.fixture
def deployers(monkeypatch, tmp_path):
    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", "x")
    monkeypatch.setenv("LOG_DIR", str(tmp_path / "logs"))
    monkeypatch.setenv("SHELL_DIR", str(tmp_path / "shell"))
    monkeypatch.setenv("CHAT_SERVICE_RESTART_SCRIPT", str(tmp_path / "fake_deploy.sh"))
    Path(tmp_path / "logs").mkdir(parents=True, exist_ok=True)
    Path(tmp_path / "shell").mkdir(parents=True, exist_ok=True)
    for m in list(sys.modules.keys()):
        if m.startswith(("config", "events", "deployers", "scheduler", "dashboard", "main")):
            sys.modules.pop(m, None)
    return importlib.import_module("deployers")


def fake_proc(rc, out="", err=""):
    """Mimics subprocess.CompletedProcess — used when we patch subprocess.run directly."""
    from types import SimpleNamespace
    return SimpleNamespace(returncode=rc, stdout=out, stderr=err)


def test_git_pull_runs_correct_cmd_and_returns_rc(deployers):
    calls = []

    def fake_run(cmd, cwd=None, env=None, capture_output=None, text=None):
        calls.append((list(cmd), cwd))
        return fake_proc(0, out="Already up to date.\n")

    steps = []
    with patch.object(deployers.subprocess, "run", side_effect=fake_run):
        rc = deployers.git_pull("/tmp/repo", lambda s: steps.append(s))

    assert rc == 0
    assert calls == [(["git", "pull", "--ff-only"], "/tmp/repo")]
    assert any("Already up to date" in s for s in steps)


def test_git_pull_returns_nonzero_on_conflict(deployers):
    with patch.object(deployers.subprocess, "run", return_value=fake_proc(1, err="conflict")):
        rc = deployers.git_pull("/tmp/repo", lambda s: None)
    assert rc == 1


def test_ensure_repo_skips_when_already_cloned(deployers, tmp_path):
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    called = []
    with patch.object(deployers.subprocess, "run", side_effect=lambda *a, **kw: called.append(a) or fake_proc(0)):
        rc = deployers.ensure_repo(str(repo), "git@x", "main", lambda s: None)
    assert rc == 0
    assert called == []


def test_ensure_repo_clones_when_missing(deployers, tmp_path):
    repo = tmp_path / "newrepo"
    calls = []
    def fake_run(cmd, **kw):
        calls.append(list(cmd))
        return fake_proc(0)
    with patch.object(deployers.subprocess, "run", side_effect=fake_run):
        rc = deployers.ensure_repo(str(repo), "git@example.com:x.git", "main", lambda s: None)
    assert rc == 0
    assert calls and calls[0][:3] == ["git", "clone", "--branch"]


def test_paths_changed_returns_true_when_first_range_matches(deployers):
    calls = []
    def fake_run(cmd, **kw):
        calls.append(cmd)
        # first range matches
        if "HEAD@{1}..HEAD" in cmd[2] if isinstance(cmd, list) else cmd[2]:
            return fake_proc(0)
        return fake_proc(1, err="")
    with patch.object(deployers.subprocess, "run", side_effect=fake_run):
        assert deployers.paths_changed("/tmp/r", ["services/deployd/"]) is True


def test_paths_changed_returns_true_on_fallback(deployers):
    def fake_run(cmd, **kw):
        # HEAD@{1} unknown revision -> continue; HEAD~1 matches
        if "HEAD@{1}" in (cmd[2] if isinstance(cmd, list) else cmd[2]):
            return fake_proc(128, err="fatal: unknown revision")
        return fake_proc(0)
    with patch.object(deployers.subprocess, "run", side_effect=fake_run):
        assert deployers.paths_changed("/tmp/r", ["services/deployd/"]) is True


def test_paths_changed_returns_false_when_both_empty(deployers):
    with patch.object(deployers.subprocess, "run", return_value=fake_proc(1, err="")):
        assert deployers.paths_changed("/tmp/r", ["nope/"]) is False


def test_write_deploy_log_creates_file(deployers, tmp_path):
    path = deployers.write_deploy_log("repoX", ["line1", "line2"])
    assert path.exists()
    assert path.read_text() == "line1\nline2"
    assert path.parent.name == "logs" or path.parent.name.endswith("logs")


def test_now_iso_is_iso8601(deployers):
    out = deployers.now_iso()
    assert "T" in out
    assert out.endswith("+00:00") or out.endswith("Z")


def test_run_deploy_sync_unknown_type_returns_1(deployers, tmp_path):
    class FakeRepo:
        path = "/tmp/x"
        branch = "main"
        url = None
        type = "weird_type"
    logs = []
    with patch.object(deployers, "git_pull", return_value=0):
        rc = deployers.run_deploy_sync("weird", FakeRepo, lambda s: logs.append(s))
    assert rc == 1
    assert any("unknown deploy type" in l for l in logs)


def test_deploy_shell_only_calls_each_step(deployers):
    seen = ["vendor", "build", "copy", "reload"]
    idx = {"i": 0}

    def step(name):
        def fn(s):
            seen.append(name)
        return fn

    with patch.object(deployers, "extract_vendor", return_value=0), \
         patch.object(deployers, "build_shell", return_value=0), \
         patch.object(deployers, "copy_shell_to_www", return_value=0), \
         patch.object(deployers, "reload_nginx", return_value=0), \
         patch.object(deployers, "deployd_changed", return_value=False), \
         patch.object(deployers, "run", return_value=fake_proc(0)):
        rc = deployers.deploy_shell_only("/tmp/repo", lambda s: None)
    assert rc == 0


def test_deploy_micro_without_rebuild_skips_shell(deployers):
    with patch.object(deployers, "run", return_value=(0, "", "")), \
         patch.object(deployers, "extract_vendor") as ev, \
         patch.object(deployers, "build_shell") as bs, \
         patch.object(deployers, "copy_shell_to_www") as cs, \
         patch.object(deployers, "reload_nginx") as rn:
        rc = deployers.deploy_micro("/tmp/r", "radio", "/tmp/build.sh", rebuild_shell=False, step=lambda s: None)
    assert rc == 0
    ev.assert_not_called()
    bs.assert_not_called()
    cs.assert_not_called()
    rn.assert_not_called()


def test_deploy_micro_with_rebuild_calls_shell(deployers):
    with patch.object(deployers, "run", return_value=(0, "", "")), \
         patch.object(deployers, "extract_vendor", return_value=0), \
         patch.object(deployers, "build_shell", return_value=0), \
         patch.object(deployers, "copy_shell_to_www", return_value=0), \
         patch.object(deployers, "reload_nginx", return_value=0):
        rc = deployers.deploy_micro("/tmp/r", "radio", "/tmp/build.sh", rebuild_shell=True, step=lambda s: None)
    assert rc == 0


def test_deploy_service_uses_provided_script(deployers):
    seen = []
    def fake_run(cmd, **kw):
        seen.append(list(cmd))
        return (0, "", "")
    with patch.object(deployers, "run", side_effect=fake_run):
        rc = deployers.deploy_service("/tmp/r", "/tmp/svc/deploy.sh", "curl -sf :9300/healthz", lambda s: None)
    assert rc == 0
    # second call = healthcheck
    assert any("curl" in cmd[2] for cmd in seen if isinstance(cmd, list) and len(cmd) >= 3)


def test_deploy_service_healthcheck_failure_returns_nonzero(deployers):
    seq = [(0, "deploy ok", ""), (1, "", "health: nope")]
    with patch.object(deployers, "run", side_effect=seq):
        rc = deployers.deploy_service("/tmp/r", "/tmp/svc/deploy.sh", "false", lambda s: None)
    assert rc != 0


def test_deploy_service_with_empty_script_returns_1(deployers):
    rc = deployers.deploy_service("/tmp/r", "", "false", lambda s: None)
    assert rc == 1


def test_copy_shell_to_www_requires_dist_dir(deployers, monkeypatch, tmp_path):
    # SHELL_DIR is set to a tmp path with no dist/ inside late-web-ui
    monkeypatch.setattr(deployers, "SHELL_DIR", str(tmp_path))
    (tmp_path / "late-web-ui").mkdir()
    seen = []
    rc = deployers.copy_shell_to_www(lambda s: seen.append(s))
    assert rc == 1
    assert any("dist/ not found" in s for s in seen)


def test_copy_shell_to_www_runs_copy_when_dist_exists(deployers, monkeypatch, tmp_path):
    shell = tmp_path / "shell"
    ui = shell / "late-web-ui"
    ui.mkdir(parents=True)
    (ui / "dist").mkdir()
    monkeypatch.setattr(deployers, "SHELL_DIR", str(shell))
    with patch.object(deployers, "run", return_value=(0, "ok", "")):
        rc = deployers.copy_shell_to_www(lambda s: None)
    assert rc == 0
