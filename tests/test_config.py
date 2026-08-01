"""Tests for DeployConfig + RepoConfig."""
from __future__ import annotations

import importlib
import sys
import time
from pathlib import Path

import pytest


@pytest.fixture
def config_mod(tmp_path, monkeypatch):
    monkeypatch.setenv("DEPOYD_CONFIG", str(tmp_path / "config.yaml"))
    for m in list(sys.modules.keys()):
        if m.startswith(("config", "events", "deployers", "scheduler", "main")):
            sys.modules.pop(m, None)
    return importlib.import_module("config")


def write_yaml(path: Path, repos: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("repos:\n" + "\n".join(
        f"  {name}:\n" + "\n".join(f"    {k}: {v!r}" for k, v in cfg.items()) + "\n"
        for name, cfg in repos.items()
    ))


def test_loads_repos_from_yaml(config_mod, tmp_path):
    write_yaml(tmp_path / "config.yaml", {
        "shell": {"path": "/root/shell", "branch": "main", "type": "shell_only"},
        "radio": {"path": "/root/radio", "type": "micro", "micro_name": "radio",
                  "build_script": "/root/build.sh", "rebuild_shell": True},
    })
    config_mod.DeployConfig(tmp_path / "config.yaml")
    cfg = config_mod.DeployConfig(tmp_path / "config.yaml")
    assert "shell" in cfg
    assert "radio" in cfg
    shell = cfg.get("shell")
    assert shell.path == "/root/shell"
    assert shell.type == "shell_only"
    radio = cfg.get("radio")
    assert radio.rebuild_shell is True
    assert radio.micro_name == "radio"


def test_defaults(config_mod, tmp_path):
    write_yaml(tmp_path / "config.yaml", {
        "x": {"path": "/tmp/x"},
    })
    cfg = config_mod.DeployConfig(tmp_path / "config.yaml")
    repo = cfg.get("x")
    assert repo.branch == "main"
    assert repo.url is None
    assert repo.type == "shell_only"
    assert repo.rebuild_shell is False
    assert repo.build_script is None
    assert repo.deploy_script is None
    assert repo.healthcheck_cmd is None
    assert repo.path == "/tmp/x"
    # default path with no /root/ prefix uses /root/{name}
    empty = config_mod.RepoConfig("y", {})
    assert empty.path == "/root/y"


def test_missing_file_is_empty(config_mod, tmp_path):
    cfg = config_mod.DeployConfig(tmp_path / "missing.yaml")
    assert cfg.repos == {}
    assert cfg.get("anything") is None
    assert "anything" not in cfg


def test_hot_reload_on_mtime_change(config_mod, tmp_path):
    yaml_path = tmp_path / "config.yaml"
    write_yaml(yaml_path, {"a": {"path": "/tmp/a"}})
    cfg = config_mod.DeployConfig(yaml_path)
    assert "a" in cfg and "b" not in cfg

    time.sleep(0.05)
    write_yaml(yaml_path, {"a": {"path": "/tmp/a"}, "b": {"path": "/tmp/b"}})
    # set newer mtime to ensure > on all fs
    yaml_path.touch()
    assert cfg.reload_if_changed() is True
    assert "b" in cfg

    # second call without change returns False
    assert cfg.reload_if_changed() is False


def test_hot_reload_no_change_returns_false(config_mod, tmp_path):
    yaml_path = tmp_path / "config.yaml"
    write_yaml(yaml_path, {"a": {"path": "/tmp/a"}})
    cfg = config_mod.DeployConfig(yaml_path)
    assert cfg.reload_if_changed() is False


def test_yaml_with_empty_repos_key(config_mod, tmp_path):
    yaml_path = tmp_path / "no.yaml"
    yaml_path.write_text("repos:\n")
    cfg = config_mod.DeployConfig(yaml_path)
    assert cfg.repos == {}


def test_yaml_root_not_a_dict(config_mod, tmp_path):
    yaml_path = tmp_path / "broken.yaml"
    yaml_path.write_text("- just\n- a\n- list\n")
    cfg = config_mod.DeployConfig(yaml_path)
    assert cfg.repos == {}


def test_repo_names(config_mod, tmp_path):
    write_yaml(tmp_path / "config.yaml", {
        "r1": {"path": "/tmp/r1"},
        "r2": {"path": "/tmp/r2"},
    })
    cfg = config_mod.DeployConfig(tmp_path / "config.yaml")
    assert sorted(cfg.repo_names) == ["r1", "r2"]


def test_repr_contains_name_and_type(config_mod, tmp_path):
    write_yaml(tmp_path / "config.yaml", {"r": {"path": "/tmp/r", "type": "micro"}})
    cfg = config_mod.DeployConfig(tmp_path / "config.yaml")
    assert "r" in repr(cfg.get("r"))
    assert "micro" in repr(cfg.get("r"))
