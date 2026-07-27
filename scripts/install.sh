#!/usr/bin/env bash
# One-shot bootstrap for late-deployd on a fresh production host.
# Idempotent: re-run safely.
set -euo pipefail

REPO_URL="${DEPLOYD_REPO_URL:-git@github.com:kodingvibes/late-deployd.git}"
INSTALL_DIR="${DEPLOYD_INSTALL_DIR:-/root/late-deployd}"
ENV_FILE="${DEPLOYD_ENV_FILE:-/root/.deployd.env}"
CONFIG_FILE="${DEPLOYD_CONFIG_FILE:-/root/.deployd/config.yaml}"
SERVICE_FILE="/etc/systemd/system/late-deployd.service"
PYTHON_BIN="${PYTHON_BIN:-/usr/bin/python3}"

bold() { printf "\033[1m%s\033[0m\n" "$*"; }
note() { printf "  · %s\n" "$*"; }

bold "[1/6] clone late-deployd to $INSTALL_DIR"
if [ ! -d "$INSTALL_DIR/.git" ]; then
  git clone "$REPO_URL" "$INSTALL_DIR"
else
  note "$INSTALL_DIR already cloned; pulling latest"
  git -C "$INSTALL_DIR" pull --ff-only
fi

bold "[2/6] install Python dependencies"
"$PYTHON_BIN" -m pip install --quiet --upgrade -r "$INSTALL_DIR/requirements.txt"

bold "[3/6] write $ENV_FILE (webhook secret)"
if [ ! -f "$ENV_FILE" ]; then
  if command -v gh >/dev/null && gh auth status >/dev/null 2>&1; then
    secret="$(gh secret list --repo kodingvibes/late-deployd --json name,value 2>/dev/null \
      | python3 -c 'import json,sys;[print(s["value"]) for s in json.load(sys.stdin) if s["name"]=="DEPLOY_WEBHOOK_SECRET"]' \
      | head -n1 || true)"
  fi
  if [ -z "${secret:-}" ]; then
    secret="$(openssl rand -hex 32)"
    echo "WARNING: generated a fresh webhook secret; wire it on GitHub manually:" >&2
    echo "  https://github.com/kodingvibes/late-deployd/settings/hooks" >&2
    echo "  secret: $secret" >&2
  fi
  cat > "$ENV_FILE" <<EOF
GITHUB_WEBHOOK_SECRET=$secret
LOG_DIR=/var/log/late-deployd
EOF
  chmod 600 "$ENV_FILE"
  note "wrote $ENV_FILE"
else
  note "$ENV_FILE already present; leaving as-is"
fi

bold "[4/6] seed $CONFIG_FILE if missing"
mkdir -p "$(dirname "$CONFIG_FILE")"
if [ ! -f "$CONFIG_FILE" ]; then
  cp "$INSTALL_DIR/config.example.yaml" "$CONFIG_FILE"
  note "wrote $CONFIG_FILE; edit to suit your host"
else
  note "$CONFIG_FILE already present; leaving as-is"
fi

bold "[5/6] install systemd unit"
cat > "$SERVICE_FILE" <<EOF
[Unit]
Description=Late auto-deploy webhook receiver
After=network.target

[Service]
Type=simple
User=root
Group=root
WorkingDirectory=$INSTALL_DIR
EnvironmentFile=$ENV_FILE
Environment=PYTHONUNBUFFERED=1
Environment=PATH=/root/.nvm/versions/node/v24.18.0/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
ExecStart=$PYTHON_BIN -m uvicorn main:APP --host 127.0.0.1 --port 9200 --proxy-headers --timeout-graceful-shutdown 5
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable late-deployd
note "unit installed and enabled"

bold "[6/6] start service"
systemctl restart late-deployd
sleep 1
systemctl --no-pager --full status late-deployd | head -n 8 || true

echo
bold "done. check:  curl -sS http://127.0.0.1:9200/health"
