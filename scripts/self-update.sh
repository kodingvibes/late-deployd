#!/usr/bin/env bash
# Self-update of late-deployd. Runs *from* late-deployd, in the deployer
# worker. Steps:
#   1. Pull origin/main.
#   2. systemctl restart late-deployd. systemd stops the old process
#      (with --timeout-graceful-shutdown 5 to bound the wait) and starts
#      the new one.
#   3. No healthcheck_cmd in the entry's config: the daemon is
#      restarting the parent of this script, so probing /health right
#      after would race with the new process. We just sleep 5s and
#      trust systemd to bring the service up; if it doesn't,
#      Restart=on-failure kicks in.
set -euo pipefail

DEPLOY_DIR="${LATE_DEPLOYD_PATH:-/root/late-deployd}"
LOG="/tmp/late-deployd-self-update.log"
echo "[self-update] start $(date -u +%FT%TZ)" | tee -a "$LOG"

cd "$DEPLOY_DIR"

echo "[self-update] pulling latest" | tee -a "$LOG"
git fetch --quiet origin main
git reset --hard --quiet origin/main

echo "[self-update] restarting service" | tee -a "$LOG"
# --no-block: don't hang the deployer worker waiting for systemd
# confirmation. The deployer has already SIGTERMed the entire
# cgroup once (via `systemctl stop late-deployd`) — here we just
# ask systemd to start a fresh instance which auto-pulls.
systemctl restart late-deployd || true

# Brief grace period; systemd's Restart=on-failure kicks in if the
# new instance dies.
sleep 5

echo "[self-update] done — service status:" | tee -a "$LOG"
systemctl is-active late-deployd | tee -a "$LOG" || true
