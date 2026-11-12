import logging
import re
import smtplib
from email.mime.text import MIMEText

import requests

from .config import (
    DISCORD_WEBHOOK_URL,
    NTFY_TOPIC,
    SMTP_HOST,
    SMTP_PORT,
    SMTP_USER,
    SMTP_PASSWORD,
    ALERT_EMAIL_TO,
)

logger = logging.getLogger('notifier')


def _strip_markdown(text):
    text = re.sub(r'\*([^*]+)\*', r'\1', text)
    text = re.sub(r'_([^_]+)_', r'\1', text)
    return text


def send_discord(text):
    if not DISCORD_WEBHOOK_URL:
        return False
    try:
        resp = requests.post(
            DISCORD_WEBHOOK_URL,
            json={"content": _strip_markdown(text)[:1900]},
            timeout=15,
        )
        if resp.status_code not in (200, 204):
            logger.warning(f"Discord HTTP {resp.status_code}: {resp.text[:200]}")
            return False
        return True
    except Exception as e:
        logger.warning(f"Discord failed: {e}")
        return False


def send_ntfy(text, title="The Agent", tags="chart", priority="high"):
    if not NTFY_TOPIC:
        return False
    try:
        resp = requests.post(
            f"https://ntfy.sh/{NTFY_TOPIC}",
            data=_strip_markdown(text).encode('utf-8'),
            headers={"Title": title, "Priority": priority, "Tags": tags},
            timeout=15,
        )
        if resp.status_code != 200:
            logger.warning(f"ntfy HTTP {resp.status_code}: {resp.text[:200]}")
            return False
        return True
    except Exception as e:
        logger.warning(f"ntfy failed: {e}")
        return False


def send_email(text):
    if not all([SMTP_HOST, SMTP_USER, SMTP_PASSWORD, ALERT_EMAIL_TO]):
        return False
    try:
        msg = MIMEText(_strip_markdown(text), 'plain', 'utf-8')
        msg['Subject'] = 'The Agent — Alert'
        msg['From'] = SMTP_USER
        msg['To'] = ALERT_EMAIL_TO
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT or 587, timeout=15) as server:
            server.starttls()
            server.login(SMTP_USER, SMTP_PASSWORD)
            server.send_message(msg)
        return True
    except Exception as e:
        logger.warning(f"Email failed: {e}")
        return False


def send_alert(text):
    """Send to all configured channels. Returns True if at least one succeeds."""
    channels = []
    if send_ntfy(text):
        channels.append('ntfy')
    if send_discord(text):
        channels.append('discord')
    if send_email(text):
        channels.append('email')

    if channels:
        logger.info(f"Alert sent via: {', '.join(channels)}")
        return True

    logger.warning("Alert failed on all channels — configure NTFY_TOPIC (see ALERTS.md)")
    return False


def _coin(symbol):
    return symbol.split('/')[0]


def _pnl_pct(side, entry, exit_price):
    if not entry or not exit_price:
        return None
    if side == 'long':
        return (exit_price - entry) / entry * 100
    return (entry - exit_price) / entry * 100


def notify_trade_entry(symbol, side, qty, price, stop_loss, take_profit, strategy):
    """Push when a limit entry order is placed."""
    text = (
        f"📈 {_coin(symbol)} {side.upper()} @ {price:.4f}\n"
        f"Size: {qty:.4f} | SL: {stop_loss} | TP: {take_profit}\n"
        f"Strategy: {strategy}"
    )
    ok = send_ntfy(text, title="Trade Open", tags="chart,money_with_wings")
    if ok:
        logger.info(f"Entry alert sent: {symbol} {side}")
    return ok


def notify_trade_exit(symbol, side, entry, exit_price, reason, strategy):
    """Push when a position is closed."""
    pnl = _pnl_pct(side, entry, exit_price)
    pnl_str = f"{pnl:+.2f}%" if pnl is not None else "n/a"
    emoji = "✅" if pnl is not None and pnl >= 0 else "❌"
    text = (
        f"{emoji} {_coin(symbol)} {side.upper()} closed @ {exit_price:.4f}\n"
        f"Entry: {entry:.4f} | P&L: {pnl_str}\n"
        f"Reason: {reason} | Strategy: {strategy}"
    )
    ok = send_ntfy(text, title="Trade Closed", tags="chart,white_check_mark" if pnl is not None and pnl >= 0 else "chart,x")
    if ok:
        logger.info(f"Exit alert sent: {symbol} {pnl_str}")
    return ok
