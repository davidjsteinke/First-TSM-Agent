#!/usr/bin/env bash
# vm_migrate.sh — migrate TSM agent to Oracle Cloud free-tier Ubuntu VM
#
# Usage:
#   VM_IP=<your-vm-ip> bash vm_migrate.sh
#
# The VM must be accessible via SSH with your default key pair.
# Run from the local machine (this machine), NOT on the VM.

set -euo pipefail

VM_IP="${VM_IP:-}"
VM_USER="${VM_USER:-ubuntu}"
REPO_URL="git@github.com:davidjsteinke/First-TSM-Agent.git"
AGENT_DIR="/home/${VM_USER}/tsm-agent"
LOCAL_ENV="$(dirname "$0")/.env"

# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------
if [[ -z "$VM_IP" ]]; then
    echo "ERROR: VM_IP is not set."
    echo "Usage: VM_IP=<ip-address> bash vm_migrate.sh"
    exit 1
fi

SSH="ssh -o StrictHostKeyChecking=no ${VM_USER}@${VM_IP}"
SCP="scp -o StrictHostKeyChecking=no"

echo "════════════════════════════════════════════════════"
echo "  TSM Agent — VM Migration"
echo "  Target: ${VM_USER}@${VM_IP}"
echo "════════════════════════════════════════════════════"

# ---------------------------------------------------------------------------
# Phase B-1: Install system dependencies on VM
# ---------------------------------------------------------------------------
echo ""
echo "[1/6] Installing system packages on VM..."
$SSH bash <<'REMOTE'
    set -euo pipefail
    export DEBIAN_FRONTEND=noninteractive
    sudo apt-get update -q
    sudo apt-get install -y -q \
        python3.11 python3.11-venv python3-pip \
        git curl ntfs-3g
    echo "System packages installed."
REMOTE

# ---------------------------------------------------------------------------
# Phase B-2: Clone the GitHub repo
# ---------------------------------------------------------------------------
echo ""
echo "[2/6] Cloning repository to VM..."
$SSH bash <<REMOTE
    set -euo pipefail
    if [ -d "${AGENT_DIR}/.git" ]; then
        echo "Repo already exists — pulling latest..."
        cd "${AGENT_DIR}" && git pull origin main
    else
        git clone "${REPO_URL}" "${AGENT_DIR}"
    fi
    echo "Repository ready at ${AGENT_DIR}"
REMOTE

# ---------------------------------------------------------------------------
# Phase B-3: Install Python dependencies
# ---------------------------------------------------------------------------
echo ""
echo "[3/6] Installing Python dependencies..."
$SSH bash <<REMOTE
    set -euo pipefail
    cd "${AGENT_DIR}"
    python3 -m pip install --quiet --upgrade pip
    python3 -m pip install --quiet -r requirements.txt
    echo "Python dependencies installed."
REMOTE

# ---------------------------------------------------------------------------
# Phase B-4: Copy .env securely to VM
# ---------------------------------------------------------------------------
echo ""
echo "[4/6] Copying .env to VM..."
if [[ ! -f "$LOCAL_ENV" ]]; then
    echo "ERROR: .env not found at ${LOCAL_ENV}"
    exit 1
fi
$SCP "$LOCAL_ENV" "${VM_USER}@${VM_IP}:${AGENT_DIR}/.env"
$SSH "chmod 600 ${AGENT_DIR}/.env"
echo ".env copied."

# ---------------------------------------------------------------------------
# Phase B-5: Set up systemd user timers on VM
# ---------------------------------------------------------------------------
echo ""
echo "[5/6] Setting up systemd timers on VM..."
$SSH bash <<REMOTE
    set -euo pipefail
    SYSTEMD_DIR="\$HOME/.config/systemd/user"
    mkdir -p "\$SYSTEMD_DIR"

    # TSM main agent timer (hourly)
    cat > "\$SYSTEMD_DIR/tsm-agent.service" <<'SVC'
[Unit]
Description=TSM Auction Agent — parse, analyse, snapshot
After=default.target

[Service]
Type=oneshot
ExecStart=/usr/bin/python3 ${AGENT_DIR}/tsm_parser.py
WorkingDirectory=${AGENT_DIR}
StandardOutput=append:${AGENT_DIR}/logs/agent.log
StandardError=append:${AGENT_DIR}/logs/agent.log
SVC

    cat > "\$SYSTEMD_DIR/tsm-agent.timer" <<'TMR'
[Unit]
Description=TSM Agent — hourly run
Requires=tsm-agent.service

[Timer]
OnBootSec=2min
OnUnitActiveSec=1h
Unit=tsm-agent.service

[Install]
WantedBy=timers.target
TMR

    # Discord alert timer (every 15 minutes)
    cat > "\$SYSTEMD_DIR/tsm-discord.service" <<'SVC'
[Unit]
Description=TSM Discord Reagent Alert
After=default.target

[Service]
Type=oneshot
ExecStart=/usr/bin/python3 ${AGENT_DIR}/discord_alerts.py
WorkingDirectory=${AGENT_DIR}
StandardOutput=append:${AGENT_DIR}/logs/agent.log
StandardError=append:${AGENT_DIR}/logs/agent.log
SVC

    cat > "\$SYSTEMD_DIR/tsm-discord.timer" <<'TMR'
[Unit]
Description=TSM Discord Alert — every 15 minutes
Requires=tsm-discord.service

[Timer]
OnBootSec=5min
OnUnitActiveSec=15min
Unit=tsm-discord.service

[Install]
WantedBy=timers.target
TMR

    systemctl --user daemon-reload
    systemctl --user enable --now tsm-agent.timer
    systemctl --user enable --now tsm-discord.timer
    echo "Timers enabled."
REMOTE

# ---------------------------------------------------------------------------
# Phase B-6: Set up Flask dashboard server
# ---------------------------------------------------------------------------
echo ""
echo "[6/6] Setting up Flask dashboard server on VM..."
$SSH bash <<REMOTE
    set -euo pipefail
    SYSTEMD_DIR="\$HOME/.config/systemd/user"

    cat > "\$SYSTEMD_DIR/tsm-dashboard.service" <<'SVC'
[Unit]
Description=TSM Dashboard — Flask HTTP server on port 5000
After=network.target

[Service]
Type=simple
ExecStart=/usr/bin/python3 -m flask --app ${AGENT_DIR}/dashboard_server.py run --host=0.0.0.0 --port=5000
WorkingDirectory=${AGENT_DIR}
Restart=always
RestartSec=5
StandardOutput=append:${AGENT_DIR}/logs/dashboard.log
StandardError=append:${AGENT_DIR}/logs/dashboard.log

[Install]
WantedBy=default.target
SVC

    # Create a minimal Flask server for the dashboard
    cat > "${AGENT_DIR}/dashboard_server.py" <<'PY'
from flask import Flask, send_file
from pathlib import Path

app = Flask(__name__)
DASHBOARD = Path(__file__).parent / "dashboard.html"

@app.route("/")
def index():
    return send_file(DASHBOARD)
PY

    systemctl --user daemon-reload
    systemctl --user enable --now tsm-dashboard.service
    echo "Dashboard server started on port 5000."
REMOTE

# ---------------------------------------------------------------------------
# Done
# ---------------------------------------------------------------------------
echo ""
echo "════════════════════════════════════════════════════"
echo "  Migration complete!"
echo "  Dashboard: http://${VM_IP}:5000"
echo "  Logs:      ${AGENT_DIR}/logs/agent.log"
echo ""
echo "  NOTE: The Lua file is NOT available on the VM."
echo "  Live AH data via Blizzard API will replace it"
echo "  as the primary data source in a future session."
echo ""
echo "  Cloudflare Tunnel (optional — run on VM after setup):"
echo "  # Install cloudflared:"
echo "  #   curl -L https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64.deb -o cf.deb"
echo "  #   sudo dpkg -i cf.deb"
echo "  # Authenticate (one-time):"
echo "  #   cloudflared tunnel login"
echo "  # Create tunnel:"
echo "  #   cloudflared tunnel create tsm-dashboard"
echo "  # Run tunnel (expose port 5000 to internet via HTTPS):"
echo "  #   cloudflared tunnel --url http://localhost:5000"
echo "════════════════════════════════════════════════════"
