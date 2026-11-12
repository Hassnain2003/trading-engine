#!/usr/bin/env bash
# Run ON the VPS as root. Installs agent to /opt/crypto_agent.
# Called by deploy/push_to_vps.sh or manually after rsync.

set -euo pipefail

AGENT_USER="${AGENT_USER:-agent}"
AGENT_DIR="/opt/crypto_agent"
SRC_DIR="${1:-/tmp/crypto_agent}"

if [[ $EUID -ne 0 ]]; then
  echo "Run as root: sudo bash $0 [source_dir]"
  exit 1
fi

echo "==> Installing packages..."
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq python3 python3-venv python3-pip rsync

# Small VMs (1GB student tiers) need swap or pip installs of the heavy
# scientific deps get OOM-killed.
if [[ ! -f /swapfile ]]; then
  echo "==> Adding 2G swapfile..."
  fallocate -l 2G /swapfile
  chmod 600 /swapfile
  mkswap /swapfile >/dev/null
  swapon /swapfile
  echo '/swapfile none swap sw 0 0' >> /etc/fstab
fi

if ! id "$AGENT_USER" &>/dev/null; then
  useradd -m -s /bin/bash "$AGENT_USER"
fi

echo "==> Deploying to $AGENT_DIR from $SRC_DIR..."
mkdir -p "$AGENT_DIR"
rsync -a --delete \
  --exclude venv --exclude __pycache__ --exclude '.git' \
  "$SRC_DIR/" "$AGENT_DIR/"
chown -R "$AGENT_USER:$AGENT_USER" "$AGENT_DIR"
chmod 600 "$AGENT_DIR/.env" 2>/dev/null || true

echo "==> Python environment..."
sudo -u "$AGENT_USER" bash -c "
  cd '$AGENT_DIR'
  python3 -m venv venv
  source venv/bin/activate
  pip install -q --upgrade pip
  pip install -q -r requirements.txt
"

echo "==> Systemd service..."
cp "$AGENT_DIR/deploy/crypto-agent.service" /etc/systemd/system/
systemctl daemon-reload
systemctl enable crypto-agent
systemctl restart crypto-agent

sleep 3
if systemctl is-active --quiet crypto-agent; then
  echo "==> Agent is RUNNING"
else
  echo "==> Agent failed to start — check logs:"
  journalctl -u crypto-agent -n 20 --no-pager
  exit 1
fi

echo "==> Alert test..."
sudo -u "$AGENT_USER" bash -c "
  cd '$AGENT_DIR' && source venv/bin/activate && python scripts/test_alerts.py
" || echo "(Alert test failed — check .env NTFY_TOPIC)"

echo ""
echo "Done. Monitor: tail -f $AGENT_DIR/agent_run.log"
