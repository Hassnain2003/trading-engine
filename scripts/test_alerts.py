#!/usr/bin/env python3
"""Test all configured alert channels."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv

load_dotenv()

from claude.config import DISCORD_WEBHOOK_URL, NTFY_TOPIC
from claude.notifier import send_discord, send_ntfy, send_alert


def main():
    configured = []
    if NTFY_TOPIC:
        configured.append(f"ntfy ({NTFY_TOPIC})")
    if DISCORD_WEBHOOK_URL:
        configured.append("discord")

    if not configured:
        print("No alert channel configured!")
        print()
        print("Recommended (free, works in Pakistan):")
        print("  1. Install ntfy app on phone: https://ntfy.sh/app")
        print("  2. Add to .env:  NTFY_TOPIC=your-secret-topic-name")
        print("  3. In app, subscribe to that topic")
        print()
        print("Or Discord webhook — see ALERTS.md")
        sys.exit(1)

    print(f"Configured: {', '.join(configured)}")
    print()

    msg = "🤖 *The Agent* — alert test OK. Trading agent is live."

    results = {
        'ntfy': send_ntfy(msg) if NTFY_TOPIC else None,
        'discord': send_discord(msg) if DISCORD_WEBHOOK_URL else None,
    }

    for name, ok in results.items():
        if ok is None:
            continue
        print(f"  {name}: {'OK' if ok else 'FAILED'}")

    if send_alert(msg):
        print("\n✅ send_alert() succeeded (at least one channel works).")
        sys.exit(0)
    print("\n❌ All channels failed.")
    sys.exit(2)


if __name__ == "__main__":
    main()
