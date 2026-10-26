#!/usr/bin/env bash
# Installs the homelab dashboard as a service on Debian / Ubuntu.
# Run from the unzipped folder:  sudo ./install.sh
set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
  echo "Run this as root:  sudo ./install.sh"
  exit 1
fi

SRC="$(cd "$(dirname "$0")" && pwd)"
DEST=/opt/homelab-dash
SERVICE=homelab-dash

echo "Checking packages..."
command -v python3 >/dev/null 2>&1 || { apt-get update -qq && apt-get install -y -qq python3; }
command -v ping >/dev/null 2>&1 || apt-get install -y -qq iputils-ping || true
# ssh + ssh-keygen let the dashboard run speed tests in another container
command -v ssh-keygen >/dev/null 2>&1 || { apt-get update -qq && apt-get install -y -qq openssh-client; } || true

id dash >/dev/null 2>&1 || useradd --system --home-dir "$DEST" --shell /usr/sbin/nologin dash

mkdir -p "$DEST"
if [ "$SRC" != "$DEST" ]; then
  cp -r "$SRC/server.py" "$SRC/static" "$SRC/config.example.json" "$SRC/README.md" "$DEST/"
fi

NEW_CONFIG=0
if [ ! -f "$DEST/config.json" ]; then
  if [ -f "$SRC/config.json" ]; then
    cp "$SRC/config.json" "$DEST/config.json"
  else
    cp "$DEST/config.example.json" "$DEST/config.json"
    NEW_CONFIG=1
  fi
fi

# The config holds your API token and iLO password, so only root and the service can read it.
chown -R root:dash "$DEST"
chmod 750 "$DEST"
chmod 640 "$DEST/config.json"
# data/ holds the speed test key and results; only the service can read it.
install -d -o dash -g dash -m 700 "$DEST/data"
chown -R dash:dash "$DEST/data"

cat > "/etc/systemd/system/$SERVICE.service" <<EOF
[Unit]
Description=Homelab dashboard
After=network-online.target
Wants=network-online.target

[Service]
User=dash
Group=dash
WorkingDirectory=$DEST
ExecStart=/usr/bin/python3 $DEST/server.py
Restart=always
RestartSec=5
AmbientCapabilities=CAP_NET_RAW CAP_NET_BIND_SERVICE
NoNewPrivileges=true

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable "$SERVICE" >/dev/null 2>&1
systemctl restart "$SERVICE"
sleep 2

PORT=$(python3 -c "import json;print(json.load(open('$DEST/config.json')).get('port',8080))" 2>/dev/null || echo 8080)
IP=$(hostname -I 2>/dev/null | awk '{print $1}')

echo
if systemctl is-active --quiet "$SERVICE"; then
  echo "Dashboard is running:  http://${IP:-this-machine}:${PORT}"
else
  echo "The service didn't start. See why with:  journalctl -u $SERVICE -n 30"
fi
if [ "$NEW_CONFIG" = 1 ]; then
  echo
  echo "Next: fill in your Proxmox token and iLO login:"
  echo "  nano $DEST/config.json"
  echo "  systemctl restart $SERVICE"
fi
