#!/usr/bin/env bash
# Self-update of late-deployd. Runs *from* late-deployd, in the deployer
# worker. Steps:
#   1. Pull origin/main (current process owns /root/late-deployd).
#   2. spawn the actual `systemctl restart` in a detached process
#      group, so that when systemd SIGTERMs our parent cgroup we
#      don't catch the same signal and get marked as failed.
#      systemd's Restart=on-failure kicks in if the new instance
#      dies. We sleep a few seconds to let it bind before returning.
set -euo pipefail

DEPLOY_DIR="${LATE_DEPLOYD_PATH:-/root/late-deployd}"
LOG="${LATE_DEPLOYD_LOG:-/tmp/late-deployd-self-update.log}"

echo "[self-update] start $(date -u +%FT%TZ)" >>"$LOG"
cd "$DEPLOY_DIR"

echo "[self-update] pulling latest" >>"$LOG"
git fetch --quiet origin main
git reset --hard --quiet origin/main

echo "[self-update] scheduling restart (detached)" >>"$LOG"
# Detached, so we don't die when systemd kills the parent cgroup.
# `setsid` puts the new process in its own session, immune to the
# cgroup SIGTERM that the restart will dispatch.
setsid bash -c "systemctl restart late-deployd" >>"$LOG" 2>&1 </dev/null &

# Brief wait so the new instance has time to bind to :9200.
sleep 5

echo "[self-update] done — status: $(systemctl is-active late-deployd)" >>"$LOG"
exit 0
