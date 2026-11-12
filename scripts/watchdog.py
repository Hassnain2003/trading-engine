#!/usr/bin/env python3
"""Watchdog for the Crypto Agent.

Checks agent.health (updated every maintenance cycle by ExecutionEngine).
If the file is missing or older than STALE_SEC, sends an alert and (optionally)
restarts the agent.

Usage:
    python scripts/watchdog.py          # check once, exit
    python scripts/watchdog.py --loop   # loop every CHECK_INTERVAL_SEC
"""

import os
import sys
import time
import subprocess
from datetime import datetime, timezone, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv()

from claude.notifier import send_alert

HEALTH_FILE = 'agent.health'
STALE_SEC = int(os.getenv('WATCHDOG_STALE_SEC', '900'))   # 15 min
CHECK_INTERVAL_SEC = int(os.getenv('WATCHDOG_CHECK_INTERVAL', '60'))
AGENT_CMD = ['bash', 'deploy/run_agent_loop.sh']
RESTART_ON_STALE = os.getenv('WATCHDOG_RESTART', '1') not in ('0', 'false', 'False')


def health_age_seconds():
    if not os.path.exists(HEALTH_FILE):
        return None
    try:
        with open(HEALTH_FILE) as f:
            line = f.read().strip()
        ts_str = line.split('|')[0]
        ts = datetime.fromisoformat(ts_str)
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - ts).total_seconds()
    except Exception:
        return None


def is_agent_running():
    try:
        out = subprocess.check_output(['pgrep', '-f', 'live/agent_runner.py'], stderr=subprocess.DEVNULL)
        return bool(out.strip())
    except subprocess.CalledProcessError:
        return False


def restart_agent():
    """Restart the agent via the loop wrapper."""
    if not is_agent_running():
        send_alert("🚑 Watchdog: agent not running — starting restart loop.")
        subprocess.Popen(AGENT_CMD, cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        return True
    return False


def check_once():
    age = health_age_seconds()
    running = is_agent_running()

    if age is None:
        msg = ("🚨 Watchdog: agent.health missing — agent may not have started yet.")
        print(msg)
        send_alert(msg)
        if RESTART_ON_STALE and not running:
            restart_agent()
        return False

    if age > STALE_SEC:
        msg = (f"🚨 Watchdog: agent health stale — {age:.0f}s old (threshold {STALE_SEC}s). "
               f"Agent running: {running}")
        print(msg)
        send_alert(msg)
        if RESTART_ON_STALE:
            restart_agent()
        return False

    print(f"✅ Watchdog: agent healthy — health {age:.0f}s old, running={running}")
    return True


def main():
    loop = '--loop' in sys.argv
    while True:
        check_once()
        if not loop:
            break
        time.sleep(CHECK_INTERVAL_SEC)


if __name__ == '__main__':
    main()