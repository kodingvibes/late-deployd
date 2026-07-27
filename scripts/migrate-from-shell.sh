#!/usr/bin/env bash
# Migrate the deploy daemon from the old in-tree location
# (`/root/late.kodingvibes.com/services/deployd/`) to its new standalone repo
# at `/root/late-deployd/`.
#
# Safe to re-run. It will not destroy state.
set -euo pipefail

OLD_DIR="${OLD_DIR:-/root/late.kodingvibes.com/services/deployd}"
NEW_DIR="${NEW_DIR:-/root/late-deployd}"
ENV_FILE="${ENV_FILE:-/root/.deployd.env}"
SERVICE_FILE="/etc/systemd/system/late-deployd.service"

bold() { printf "\033[1m%s\033[0m\n" "$*"; }
note() { printf "  · %s\n" "$*"; }

if [ ! -d "$OLD_DIR" ]; then
  echo "$OLD_DIR does not exist; nothing to migrate from." >&2
  exit 1
fi

if [ ! -d "$NEW_DIR/.git" ]; then
  bold "cloning late-deployd to $NEW_DIR"
  git clone git@github.com:kodingvibes/late-deployd.git "$NEW_DIR"
fi

bold "[1/5] install Python deps for the new location"
PYTHON_BIN="${PYTHON_BIN:-/usr/bin/python3}"
"$PYTHON_BIN" -m pip install --quiet --upgrade -r "$NEW_DIR/requirements.txt" --break-system-packages

bold "[2/5] seed /root/.deployd/config.yaml if missing"
mkdir -p /root/.deployd
if [ ! -f /root/.deployd/config.yaml ]; then
  cp "$NEW_DIR/config.example.yaml" /root/.deployd/config.yaml
  note "wrote /root/.deployd/config.yaml"
else
  note "/root/.deployd/config.yaml already present; leaving as-is"
fi

bold "[3/5] rewrite systemd unit to point at the new location"
cat > "$SERVICE_FILE" <<EOF
[Unit]
Description=Late auto-deploy webhook receiver
After=network.target

[Service]
Type=simple
User=root
Group=root
WorkingDirectory=$NEW_DIR
EnvironmentFile=$ENV_FILE
Environment=PYTHONUNBUFFERED=1
Environment=PATH=/root/.nvm/versions/node/v24.18.0/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
ExecStart=/bin/bash -c 'cd $NEW_DIR && exec $PYTHON_BIN -m uvicorn main:APP --host 127.0.0.1 --port 9200 --proxy-headers'
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
note "wrote $SERVICE_FILE"

bold "[4/5] restart service from the new working dir"
systemctl restart late-deployd
sleep 1
if curl -fsS -m 3 http://127.0.0.1:9200/health >/dev/null; then
  note "service is up: $(curl -fsS -m 3 http://127.0.0.1:9200/health | tr -d '\n')"
else
  echo "service did not respond to /health; check:  systemctl status late-deployd" >&2
  exit 1
fi

bold "[5/5] remove old code from $OLD_DIR"
note "deleting $OLD_DIR now — safe because the new install is live"
rm -rf "$OLD_DIR"
note "leaving the old git history intact in /root/late.kodingvibes.com; the directory tree simply no longer contains services/deployd/"

echo
bold "migration complete. next steps:"
echo "  - delete /root/late.kodingvibes.com/services/ from the next deploy"
echo "  - commit the removal in this repo: git -C /root/late.kodingvibes.com rm -r services/deployd && git commit"
echo "  - on the next push of late.kodingvibes.com, the deploy hook for the daemon repo (if any) will update again"
