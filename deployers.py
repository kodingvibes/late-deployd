from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

logger = logging.getLogger("deployd")

SHELL_DIR = "/root/late.kodingvibes.com"
LOG_DIR = Path(os.environ.get("LOG_DIR", "/var/log/late-deployd"))
SECRET = os.environ.get("GITHUB_WEBHOOK_SECRET", "").encode()
CHAT_SERVICE_RESTART_SCRIPT = os.environ.get(
    "CHAT_SERVICE_RESTART_SCRIPT",
    "/root/late-chat-service/scripts/deploy.sh",
)


def _env() -> dict:
    return {"PATH": "/root/.nvm/versions/node/v24.18.0/bin:" + os.environ.get("PATH", "")}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def run(cmd: list[str], cwd: Optional[str] = None, extra_env: Optional[dict] = None) -> tuple[int, str, str]:
    env = {**os.environ, **_env(), **(extra_env or {})}
    proc = subprocess.run(cmd, cwd=cwd, env=env, capture_output=True, text=True)
    return proc.returncode, proc.stdout, proc.stderr


def git_pull(repo_path: str, step: Callable[[str], None]) -> int:
    step(f"[{now_iso()}] git pull --ff-only in {repo_path}")
    rc, out, err = run(["git", "pull", "--ff-only"], cwd=repo_path)
    step(out.rstrip())
    if err:
        step(f"stderr: {err.rstrip()}")
    step(f"exit code: {rc}")
    return rc


def ensure_repo(path: str, url: str, branch: str, step: Callable[[str], None]) -> int:
    if os.path.isdir(os.path.join(path, ".git")):
        return 0
    step(f"[{now_iso()}] cloning {url} -> {path} (branch {branch})")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    rc, out, err = run(["git", "clone", "--branch", branch, url, path])
    step(out.rstrip())
    if err:
        step(f"stderr: {err.rstrip()}")
    step(f"clone exit code: {rc}")
    return rc


def paths_changed(repo_path: str, prefixes: list[str]) -> bool:
    ranges = ["HEAD@{1}..HEAD", "HEAD~1..HEAD"]
    prefix_filter = "|".join(f"^{p}" for p in prefixes)
    for rng in ranges:
        rc, out, err = run(
            ["bash", "-c", f"git diff --name-only {rng} | grep -qE '{prefix_filter}'"],
            cwd=repo_path,
        )
        if rc == 0:
            return True
        if "HEAD@{1}" in rng and "unknown revision" in (err or ""):
            continue
        break
    return False


def deployd_changed(repo_path: str) -> bool:
    return paths_changed(repo_path, ["services/deployd/"])


def extract_vendor(step: Callable[[str], None]) -> int:
    step(f"[{now_iso()}] extract vendor")
    rc, out, err = run(["bash", f"{SHELL_DIR}/scripts/extract-vendor.sh"])
    step(out.rstrip())
    if err:
        step(f"stderr: {err.rstrip()}")
    if rc != 0:
        step(f"vendor extract failed: {rc}")
    return rc


def build_shell(step: Callable[[str], None]) -> int:
    step(f"[{now_iso()}] build shell")
    ui_dir = f"{SHELL_DIR}/late-web-ui"
    rc, out, err = run(["bash", "-c", "npm run build"], cwd=ui_dir)
    step(out.rstrip())
    if err:
        step(f"stderr: {err.rstrip()}")
    if rc != 0:
        step(f"shell build failed: {rc}")
    return rc


def copy_shell_to_www(step: Callable[[str], None]) -> int:
    step(f"[{now_iso()}] copy dist to /var/www/html")
    ui_dir = f"{SHELL_DIR}/late-web-ui"
    dist = os.path.join(ui_dir, "dist")
    if not os.path.isdir(dist):
        step(f"[error] dist/ not found at {dist}; was build_shell() successful?")
        return 1
    rc, out, err = run(
        ["bash", "-c", "rm -rf /var/www/html/assets /var/www/html/index.html && cp -r dist/. /var/www/html/"],
        cwd=ui_dir,
    )
    step(out.rstrip())
    if err:
        step(f"stderr: {err.rstrip()}")
    if rc != 0:
        step(f"copy failed: {rc}")
    return rc


def reload_nginx(step: Callable[[str], None]) -> int:
    step(f"[{now_iso()}] reload nginx")
    rc, out, err = run(["nginx", "-s", "reload"])
    step(out.rstrip())
    if err:
        step(f"stderr: {err.rstrip()}")
    if rc != 0:
        step(f"nginx reload failed: {rc}")
    return rc


def write_deploy_log(repo_name: str, lines: list[str]) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    log_file = LOG_DIR / f"{repo_name}-{stamp}.log"
    log_file.write_text("\n".join(lines), encoding="utf-8")
    logger.info("deploy log written to %s", log_file)
    return log_file


def verify_signature(body: bytes, signature: Optional[str]) -> bool:
    if not SECRET:
        logger.error("GITHUB_WEBHOOK_SECRET not configured")
        return False
    if not signature:
        return False
    prefix = "sha256="
    if not signature.startswith(prefix):
        return False
    expected = signature[len(prefix):].encode()
    digest = hmac.new(SECRET, body, hashlib.sha256).hexdigest().encode()
    return hmac.compare_digest(digest, expected)


# ---------------------------------------------------------------------------
# Deployers — each returns (exit_code, log_lines)
# ---------------------------------------------------------------------------

def deploy_shell_only(repo_path: str, step: Callable[[str], None]) -> int:
    if extract_vendor(step) != 0:
        return 1
    if build_shell(step) != 0:
        return 1
    if copy_shell_to_www(step) != 0:
        return 1
    if reload_nginx(step) != 0:
        return 1
    if deployd_changed(repo_path):
        step(f"[{now_iso()}] deployd code changed; scheduling self-restart")
        run(["systemctl", "restart", "late-deployd"])
    return 0


def deploy_micro(repo_path: str, micro_name: str, build_script: str, rebuild_shell: bool, step: Callable[[str], None]) -> int:
    step(f"[{now_iso()}] build micro {micro_name}")
    rc, out, err = run(["bash", build_script])
    step(out.rstrip())
    if err:
        step(f"stderr: {err.rstrip()}")
    if rc != 0:
        step(f"micro {micro_name} build failed: {rc}")
        return rc
    if rebuild_shell:
        step(f"[{now_iso()}] micro {micro_name} ready; rebuilding shell")
        if extract_vendor(step) != 0:
            return 1
        if build_shell(step) != 0:
            return 1
        if copy_shell_to_www(step) != 0:
            return 1
        if reload_nginx(step) != 0:
            return 1
    return 0


def deploy_service(repo_path: str, deploy_script: str, healthcheck_cmd: Optional[str], step: Callable[[str], None]) -> int:
    if not deploy_script:
        step(f"[error] no deploy_script configured for {repo_path}")
        return 1
    step(f"[{now_iso()}] running deploy script: {deploy_script}")
    rc, out, err = run(["bash", "-c", deploy_script])
    step(out.rstrip())
    if err:
        step(f"stderr: {err.rstrip()}")
    if rc != 0:
        step(f"deploy script failed: {rc}")
        return rc
    if healthcheck_cmd:
        step(f"[{now_iso()}] healthcheck")
        rc, out, err = run(["bash", "-c", healthcheck_cmd])
        if rc != 0:
            step(f"healthcheck failed: {err.rstrip()}")
        else:
            step("healthcheck passed")
    return rc


def run_deploy_sync(repo_name: str, config, step: Callable[[str], None]) -> int:
    log: list[str] = []
    def _step(line: str) -> None:
        log.append(line)
        step(line)

    if config.url and not os.path.isdir(os.path.join(config.path, ".git")):
        rc = ensure_repo(config.path, config.url, config.branch, _step)
        if rc != 0:
            _step(f"[{now_iso()}] clone failed, aborting")
            write_deploy_log(repo_name, log)
            return 1

    rc = git_pull(config.path, _step)
    if rc != 0:
        _step(f"[{now_iso()}] git pull failed, aborting")
        write_deploy_log(repo_name, log)
        return 1

    if config.type == "shell_only":
        rc = deploy_shell_only(config.path, _step)
    elif config.type == "micro":
        rc = deploy_micro(config.path, config.micro_name or repo_name, config.build_script or "", config.rebuild_shell, _step)
    elif config.type == "service":
        rc = deploy_service(config.path, config.deploy_script or "", config.healthcheck_cmd, _step)
    else:
        _step(f"[{now_iso()}] unknown deploy type: {config.type}")
        rc = 1

    _step(f"[{now_iso()}] deploy finished with code {rc}")
    write_deploy_log(repo_name, log)
    return rc