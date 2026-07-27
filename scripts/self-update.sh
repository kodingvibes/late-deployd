#!/usr/bin/env bash
# Self-update of late-deployd. Runs *from* late-deployd, in the deployer
# worker. Steps:
#   1. systemctl restart late-deployd  (kills this script's parent if
#      they share a session — that's fine, the script is already past
#      the dangerous part).
#   2. After restart, the daemon's /health probe (in the deployer's
#      `healthcheck_cmd` in /root/.deployd/config.yaml) verifies the
#      new process is up.
#
# Notes:
#   - The deployer reads $LATE_DEPLOYD_PATH (set by the daemon via env)
#     so this script doesn't hardcode its own path. If env is missing
#     we fall back to the canonical path.
set -euo pipefail

DEPLOY_DIR="${LATE_DEPLOYD_PATH:-/root/late-deployd}"
LOG="/tmp/late-deployd-self-update.log"
echo "[self-update] start $(date -u +%FT%TZ)" | tee -a "$LOG"

cd "$DEPLOY_DIR"

echo "[self-update] pulling latest" | tee -a "$LOG"
git fetch --quiet origin main
git reset --hard --quiet origin/main

echo "[self-update] restarting service" | tee -a "$LOG"
systemctl restart late-deployd

echo "[self-update] done" | tee -a "$LOG"
