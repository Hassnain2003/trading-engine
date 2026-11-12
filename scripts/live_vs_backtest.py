#!/usr/bin/env python3
"""Print the live-vs-backtest report. Run any time:

    source venv/bin/activate && python scripts/live_vs_backtest.py [--since 2026-07-08]
"""

import os
import sys
import sqlite3
import argparse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from utils.live_tracker import build_report
from trading_phase import DB_FILENAME

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument('--since', help='ISO date, e.g. 2026-07-08', default=None)
    args = parser.parse_args()

    db = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), DB_FILENAME)
    conn = sqlite3.connect(db)
    print(build_report(conn, since=args.since))
