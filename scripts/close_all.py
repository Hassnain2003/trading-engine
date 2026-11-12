#!/usr/bin/env python3
"""Manually close positions and stand the agent down on them for the rest of
the UTC day (re-entry only after the next daily verdict).

    python scripts/close_all.py            # close everything
    python scripts/close_all.py BTC ETH    # close only these coins

The running agent picks up the standdown flags immediately (they live in the
shared DB) — no restart needed. Its reconciler will detect the closes, update
the trade records, and send the usual "Trade Closed" alerts with P&L.
"""

import os
import sys
import sqlite3

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
import ccxt

from utils.manual_close import close_positions
from trading_phase import DB_FILENAME

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
load_dotenv(os.path.join(ROOT, '.env'))


def main():
    only = {c.upper() for c in sys.argv[1:]} or None
    demo = ccxt.bybit({
        'enableRateLimit': True, 'timeout': 20000,
        'apiKey': os.getenv('BYBIT_API_KEY'),
        'secret': os.getenv('BYBIT_API_SECRET'),
        'options': {'defaultType': 'linear', 'recvWindow': 20000},
    })
    demo.set_sandbox_mode(True)
    conn = sqlite3.connect(os.path.join(ROOT, DB_FILENAME))

    results = close_positions(demo, conn, coins=only)
    if not results:
        print("No matching open positions.")
        return
    total = 0.0
    for r in results:
        if r['ok']:
            total += r['upnl']
            print(f"CLOSED {r['side']} {r['qty']} {r['symbol']} (uPnL was {r['upnl']:+.2f}) — standdown until next UTC day")
        else:
            print(f"FAILED to close {r['symbol']}: {r['error']}")
    print(f"\nApprox realized: {total:+.2f} USDT (exact P&L arrives via Trade Closed alerts).")


if __name__ == '__main__':
    main()
