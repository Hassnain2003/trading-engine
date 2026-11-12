#!/usr/bin/env bash
# One-command deploy from your laptop to a VPS.
# Usage:  bash deploy/push_to_vps.sh ubuntu@YOUR_VPS_IP
#
# Prerequisites:
#   - Ubuntu 22.04+ VPS (Oracle Cloud Singapore recommended)
#   - SSH key access:  ssh ubuntu@YOUR_VPS_IP  works without password
#   - .env file present in project root

set -euo pipefail

REMOTE="${1:-}"
PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"

if [[ -z "$REMOTE" ]]; then
  echo "Usage: bash deploy/push_to_vps.sh user@VPS_IP"
  echo ""
  echo "Don't have a VPS yet?"
  echo "  1. Go to https://cloud.oracle.com → Create free VM (Ubuntu, Singapore)"
  echo "  2. Add your SSH public key during setup"
  echo "  3. Open port 22 in Security List / firewall"
  echo "  4. Run this script again"
  exit 1
fi

if [[ ! -f "$PROJECT_DIR/.env" ]]; then
  echo "Missing $PROJECT_DIR/.env — copy your API keys there first."
  exit 1
fi

echo "==> Testing SSH to $REMOTE..."
if ! ssh -o ConnectTimeout=10 -o BatchMode=yes "$REMOTE" "echo ok" &>/dev/null; then
  echo "Cannot SSH to $REMOTE"
  echo "  - Check IP and username (usually ubuntu@... or opc@... for Oracle)"
  echo "  - Ensure your SSH key is on the VPS"
  exit 1
fi

echo "==> Uploading project to /tmp/crypto_agent..."
rsync -avz --delete \
  --exclude venv --exclude __pycache__ --exclude '.git' \
  --exclude 'agent_trades.db' --exclude 'agent_run.log' \
  "$PROJECT_DIR/" "$REMOTE:/tmp/crypto_agent/"

echo "==> Running remote install (needs sudo on VPS)..."
ssh -t "$REMOTE" "sudo bash /tmp/crypto_agent/deploy/install_vps.sh /tmp/crypto_agent"

echo ""
echo "==> Stopping LOCAL agent (avoid duplicate trading)..."
pkill -f "run_agent_loop.sh" 2>/dev/null || true
pkill -f "live/agent_runner.py" 2>/dev/null && echo "Local agent stopped." || echo "No local agent running."

echo ""
echo "============================================"
echo "  Deploy complete. Agent runs on VPS 24/7."
echo "  Logs:  ssh $REMOTE 'tail -f /opt/crypto_agent/agent_run.log'"
echo "  Status: ssh $REMOTE 'sudo systemctl status crypto-agent'"
echo "============================================"
