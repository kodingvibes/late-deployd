#!/usr/bin/env bash
# Self-update of late-deployd. Runs *from* late-deployd, in the deployer
# worker. Steps:
#   1. Pull origin/main in this process — it owns /root/late-deployd.
#   2. Schedule `systemctl restart late-deployd` via nohup+setsid.
#      The detached process re-execs with stdin/stdout/stderr closed so
#      it survives even if systemd SIGTERMs its parent cgroup.
#   3. systemd brings up the new instance; its Restart=on-failure
#      catches any startup crash.
#   4. We sleep a few seconds and return success. The deployer reports
#      success even if the detached restart races; that is fine because
#      the daemon will be up either way within a few seconds.
set -euo pipefail

DEPLOY_DIR="${LATE_DEPLOYD_PATH:-/root/late-deployd}"
LOG="${LATE_DEPLOYD_LOG:-/tmp/late-deployd-self-update.log}"

echo "[self-update] start $(date -u +%FT%TZ)" >>"$LOG"

cd "$DEPLOY_DIR"
echo "[self-update] pulling latest" >>"$LOG"
git fetch --quiet origin main
git reset --hard --quiet origin/main

echo "[self-update] scheduling restart (nohup setsid)" >>"$LOG"

# Use a separate script file so we can disown it cleanly.
# The detached script is responsible for the actual `systemctl restart`.
cat >/tmp/late-deployd-restart.sh <<'RESTART'
#!/usr/bin/env bash
exec >>"$0.log" 2>&1
set -e
echo "[detached-restart] start $(date -u +%FT%TZ)"
# Wait briefly so the parent's deployer worker finishes and the
# subprocess group reaps its children before systemd kills the cgroup.
sleep 3
echo "[detached-restart] running systemctl restart"
systemctl restart late-deployd
sleep 5
echo "[detached-restart] status: $(systemctl is-active late-deployd)"
exit 0
RESTART
chmod +x /tmp/late-deployd-restart.sh

# setsid: new session/pgid; nohup: immune to SIGHUP; </dev/null: no
# stdin to leak; & + disown: not a job we own.
setsid nohup /tmp/late-deployd-restart.sh </dev/null >/dev/null 2>&1 &
disown || true

# Give the new instance time to bind :9200.
sleep 10

echo "[self-update] done at $(date -u +%FT%TZ) — service: $(systemctl is-active late-deployd)" >>"$LOG"
exit 0
