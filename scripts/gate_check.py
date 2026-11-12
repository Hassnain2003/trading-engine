#!/usr/bin/env python3
"""Show go-live gate progress on clean (venue-filtered) trades.

    python scripts/gate_check.py
"""

import os
import sys
import sqlite3

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from trading_phase import DB_FILENAME
from utils.stats import gate_status

DB = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), DB_FILENAME)

if __name__ == "__main__":
    g = gate_status(sqlite3.connect(DB))
    print("=" * 52)
    print("  GO-LIVE GATE" + ("  🚀 ALL CRITERIA PASSED" if g['passed'] else ""))
    print("=" * 52)
    for key, c in g['checks'].items():
        mark = "✅" if c['ok'] else "❌"
        print(f"  {mark} {key.replace('_', ' '):18s} {c['value']:>8}  (need {c['op']} {c['threshold']})")
    print(f"\n  Trade progress: {g['metrics']['total_trades']}/60 "
          f"[{'#' * int(g['trades_progress'] * 20):<20}] {g['trades_progress']*100:.0f}%")
    if not g['passed']:
        print("  Gate not passed yet — keep collecting evidence.")
