# Deploy The Agent on a VPS (recommended for 4-week demo)

Telegram is blocked in Pakistan. ntfy push alerts work without VPN and give 24/7 uptime on a VPS without laptop VPN.

## 1. Get a free VPS

| Provider | Region | Notes |
|----------|--------|-------|
| [Oracle Cloud](https://www.oracle.com/cloud/free/) | Singapore / Japan | Always-free ARM, best option |
| [Google Cloud](https://cloud.google.com/free) | asia-southeast1 | $300 trial |
| [Hetzner](https://www.hetzner.com/cloud) | EU | ~€4/mo if no free tier |

Create an **Ubuntu 22.04** instance. Open SSH (port 22).

## 2. Bootstrap the VPS

```bash
ssh ubuntu@YOUR_VPS_IP
sudo bash -c "$(curl -fsSL https://raw.githubusercontent.com/...)"  # or copy deploy/install_vps.sh
```

Or copy `deploy/install_vps.sh` and run:

```bash
sudo bash install_vps.sh
```

## 3. Upload the project (from your laptop)

```bash
rsync -avz --exclude venv --exclude __pycache__ --exclude agent_trades.db \
  ~/Documents/crypto_agent/ agent@YOUR_VPS_IP:/opt/crypto_agent/
```

Copy `.env` separately (contains secrets):

```bash
scp ~/Documents/crypto_agent/.env agent@YOUR_VPS_IP:/opt/crypto_agent/.env
```

## 4. Install deps on VPS

```bash
ssh agent@YOUR_VPS_IP
cd /opt/crypto_agent
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

## 5. Start as a service

```bash
sudo cp /opt/crypto_agent/deploy/crypto-agent.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now crypto-agent
sudo systemctl status crypto-agent
tail -f /opt/crypto_agent/agent_run.log
```

## 6. Test alerts (on VPS)

```bash
cd /opt/crypto_agent && source venv/bin/activate
python scripts/test_alerts.py
```

---

## Option A: Keep agent on laptop, run 24/7 on VPS

If you want alerts and uptime without keeping your laptop on, deploy to a VPS (see above). ntfy works from anywhere — no VPN needed.

---

## Stop local agent before starting VPS

Don't run two agents with the same API keys:

```bash
pkill -f agent_runner.py   # on laptop
```
