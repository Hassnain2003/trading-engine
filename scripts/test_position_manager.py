#!/usr/bin/env python3
"""Smoke test S3 trailing stop module import and exchange connectivity."""

import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv

load_dotenv()

import ccxt
from execution.position_manager import update_s3_trailing_stops


def main():
    key = os.getenv('BYBIT_API_KEY')
    secret = os.getenv('BYBIT_API_SECRET')
    if not key or not secret:
        print("FAIL: BYBIT_API_KEY / BYBIT_API_SECRET missing in .env")
        sys.exit(1)

    public = ccxt.bybit({'options': {'defaultType': 'linear'}})
    demo = ccxt.bybit({
        'apiKey': key,
        'secret': secret,
        'options': {'defaultType': 'linear', 'recvWindow': 20000},
    })
    demo.set_sandbox_mode(True)

    print("1. Testing Bybit testnet connection...")
    try:
        demo.load_time_difference()
        balance = demo.fetch_balance()
        usdt = balance.get('USDT', {}).get('total', 0)
        print(f"   OK — USDT balance: {usdt}")
    except Exception as e:
        print(f"   FAIL: {e}")
        sys.exit(2)

    print("2. Running update_s3_trailing_stops() once...")
    conn = sqlite3.connect(':memory:')
    conn.execute('''CREATE TABLE signals (
        id INTEGER PRIMARY KEY, symbol TEXT, strategy TEXT, status TEXT)''')
    try:
        positions = [
            p for p in demo.fetch_positions()
            if float(p.get('contracts', 0) or 0) != 0
        ]
        update_s3_trailing_stops(demo, public, positions, conn)
        print(f"   OK — checked {len(positions)} open position(s)")
    except Exception as e:
        print(f"   FAIL: {e}")
        sys.exit(3)

    print("\nPASS — S3 trailing stop module ready.")
    sys.exit(0)


if __name__ == '__main__':
    main()
