#!/usr/bin/env bash
set -e

echo "================================================================"
echo "  AgentIC On-Premises Enterprise Server Deployment"
echo "  100% Zero-Cloud - Confidential Semiconductor IP Protection"
echo "================================================================"

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

PORT="${PORT:-7860}"
echo "[1/3] Checking Python 3 dependencies..."
python3 -m pip install -q -r requirements.txt || true

echo "[2/3] Starting AgentIC On-Prem Server background daemon..."
if command -v systemctl &>/dev/null && [ -w /etc/systemd/system ]; then
    cat <<EOF > /etc/systemd/system/agentic-server.service
[Unit]
Description=AgentIC On-Prem Enterprise Chip Design Server
After=network.target

[Service]
Type=simple
User=$USER
WorkingDirectory=$SCRIPT_DIR
ExecStart=$(which python3) $SCRIPT_DIR/main.py
Restart=on-failure
Environment=PORT=$PORT

[Install]
WantedBy=multi-user.target
EOF
    systemctl daemon-reload
    systemctl enable agentic-server
    systemctl restart agentic-server
    echo "✓ Installed and started systemd service 'agentic-server'"
else
    nohup python3 main.py > server.log 2>&1 &
    echo "✓ Started background server daemon (PID: $!). Logs: $SCRIPT_DIR/server.log"
fi

echo "[3/3] Deployment successful!"
echo "----------------------------------------------------------------"
echo "On-Prem Server URL: http://$(hostname -I | awk '{print $1}'):$PORT"
echo "Engineers can connect desktop apps by setting:"
echo "export AGENTIC_LOCAL_URL=http://$(hostname -I | awk '{print $1}'):$PORT"
echo "================================================================"
