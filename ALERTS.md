# Agent alerts (Pakistan-friendly)

The agent needs **one** alert channel. Trading works regardless.

## Option 1: ntfy (recommended)

**Cost:** Free | **Card:** No | **VPN:** No

1. Install **ntfy** on your phone: https://ntfy.sh/app  
   (or use https://ntfy.sh in browser)
2. Pick a secret topic name, e.g. `<your-secret-topic>` (do not reuse a real topic).
3. Add to `.env`:
   ```
   NTFY_TOPIC=<your-secret-topic>
   ```
4. In the app: **Subscribe to topic** → enter the same name
5. Test:
   ```bash
   python scripts/test_alerts.py
   ```

You get push notifications on your phone for every agent alert.

---

## Option 2: Discord webhook

**Cost:** Free | **Card:** No | **VPN:** No

1. Open Discord → create a server (or use existing)
2. Server Settings → Integrations → **Webhooks** → New Webhook
3. Copy webhook URL
4. Add to `.env`:
   ```
   DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/...
   ```
5. Test: `python scripts/test_alerts.py`

---

## Option 3: Email (Gmail)

1. Enable 2FA on Gmail → create **App Password**
2. Add to `.env`:
   ```
   SMTP_HOST=smtp.gmail.com
   SMTP_PORT=587
   SMTP_USER=your@gmail.com
   SMTP_PASSWORD=your-16-char-app-password
   ALERT_EMAIL_TO=your@gmail.com
   ```

---

## Test command

```bash
cd <your-project-folder>
source venv/bin/activate
python scripts/test_alerts.py
```

The agent sends to **all configured channels** each time.
