#!/usr/bin/env bash
# Self-update of late-deployd. Runs *from* late-deployd, in the deployer
# worker.
#
# Race contract:
#   - This script is a child of uvicorn's cgroup. When the deployer
#     returns, the next batch of deploys (or a manual `systemctl
#     restart`) will SIGTERM the cgroup. We must be gone by then or
#     accept SIGTERM.
#   - The actual `systemctl restart late-deployd` is handed off to a
#     detached helper (setsid + nohup) that lives in its own session.
#     It survives the cgroup kill and brings up the new instance.
#   - The /root/.deployd/config.yaml entry for late-deployd has no
#     healthcheck_cmd, because the parent deployer always exits before
#     the new instance is bound. The deploy is recorded as
#     "deploy.failure" with rc=-15 in /var/log/late-deployd/. That is
#     expected. The /health endpoint reports the new state once it's
#     up.
#
# Steps in this script (kept under ~1s total so we exit before the
# parent cgroup receives SIGTERM from the *next* orchestration step):
#   1. Pull origin/main.
#   2. Write the helper script.
#   3. Spawn it detached with setsid+nohup.
#   4. Return 0 immediately.
set -euo pipefail

DEPLOY_DIR="${LATE_DEPLOYD_PATH:-/root/late-deployd}"
LOG="${LATE_DEPLOYD_LOG:-/tmp/late-deployd-self-update.log}"
RESTART_HELPER="${LATE_DEPLOYD_RESTART_HELPER:-/tmp/late-deployd-restart.sh}"

{
  echo "[self-update] start $(date -u +%FT%TZ)"
  cd "$DEPLOY_DIR"
  echo "[self-update] git fetch/reset"
  git fetch --quiet origin main
  git reset --hard --quiet origin/main

  cat >"$RESTART_HELPER" <<'HELPER'
#!/usr/bin/env bash
set -euo pipefail
LOG="${LATE_DEPLOYD_LOG:-/tmp/late-deployd-self-update.log}"
echo "[detached-restart] start $(date -u +%FT%TZ)" >>"$LOG"
sleep 1
systemctl restart late-deployd
sleep 5
echo "[detached-restart] done — status: $(systemctl is-active late-deployd)" >>"$LOG"
HELPER
  chmod +x "$RESTART_HELPER"

  echo "[self-update] spawning detached helper"
  setsid nohup "$RESTART_HELPER" </dev/null >/dev/null 2>&1 &
  disown || true

  echo "[self-update] exit 0"
} >>"$LOG" 2>&1

exit 0
