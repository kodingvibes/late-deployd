from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any, Optional

try:
    import yaml
except ImportError:
    yaml = None

CONFIG_PATH = Path(os.environ.get("DEPOYD_CONFIG", "/root/.deployd/config.yaml"))


class RepoConfig:
    def __init__(self, name: str, data: dict):
        self.name = name
        self.path: str = data.get("path", f"/root/{name}")
        self.branch: str = data.get("branch", "main")
        self.url: Optional[str] = data.get("url")
        self.type: str = data.get("type", "shell_only")
        # micro
        self.micro_name: Optional[str] = data.get("micro_name")
        self.build_script: Optional[str] = data.get("build_script")
        self.rebuild_shell: bool = data.get("rebuild_shell", False)
        # service
        self.deploy_script: Optional[str] = data.get("deploy_script")
        self.healthcheck_url: Optional[str] = data.get("healthcheck_url")
        self.healthcheck_cmd: Optional[str] = data.get("healthcheck_cmd")

    @property
    def repo_path(self) -> str:
        return self.path

    def __repr__(self) -> str:
        return f"<RepoConfig {self.name} type={self.type}>"


class DeployConfig:
    def __init__(self, path: Path = CONFIG_PATH):
        self._path = path
        self._repos: dict[str, RepoConfig] = {}
        self._mtime: float = 0.0
        self._load()

    def _load(self) -> None:
        if not self._path.exists():
            return
        self._mtime = self._path.stat().st_mtime
        if yaml is None:
            return
        raw = self._path.read_text(encoding="utf-8")
        data = yaml.safe_load(raw) or {}
        if not isinstance(data, dict):
            self._repos = {}
            return
        raw_repos = data.get("repos") or {}
        if not isinstance(raw_repos, dict):
            self._repos = {}
            return
        self._repos = {
            name: RepoConfig(name, cfg or {})
            for name, cfg in raw_repos.items()
        }

    def reload_if_changed(self) -> bool:
        try:
            mtime = self._path.stat().st_mtime if self._path.exists() else 0.0
        except OSError:
            return False
        if mtime > self._mtime:
            self._load()
            return True
        return False

    def get(self, name: str) -> Optional[RepoConfig]:
        return self._repos.get(name)

    def __contains__(self, name: str) -> bool:
        return name in self._repos

    @property
    def repos(self) -> dict[str, RepoConfig]:
        return dict(self._repos)

    @property
    def repo_names(self) -> list[str]:
        return list(self._repos.keys())