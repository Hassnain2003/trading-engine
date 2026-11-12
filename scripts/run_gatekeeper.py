#!/usr/bin/env python3
"""Run go-live gatekeeper: compute demo metrics from DB and send Gemini decision via ntfy."""

import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv

load_dotenv()

from claude.jobs import go_live_gatekeeper
from utils.stats import compute_demo_metrics
from trading_phase import DB_FILENAME


def main():
    db_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), DB_FILENAME)
    if not os.path.exists(db_path):
        print(f"No database at {db_path} — run the demo first.")
        sys.exit(1)

    conn = sqlite3.connect(db_path)
    metrics = compute_demo_metrics(conn)
    conn.close()

    print("Demo metrics:")
    for k, v in metrics.items():
        print(f"  {k}: {v}")

    gates = {
        'PF >= 1.3': metrics['profit_factor'] >= 1.3,
        'Sharpe >= 1.0': metrics['sharpe_ratio'] >= 1.0,
        'MaxDD <= 15%': metrics['max_drawdown_pct'] <= 15.0,
        'Trades >= 60': metrics['total_trades'] >= 60,
        'WR 45-62%': 45.0 <= metrics['win_rate_pct'] <= 62.0,
    }
    print("\nGate checklist:")
    for gate, passed in gates.items():
        print(f"  {'PASS' if passed else 'FAIL'}  {gate}")

    print("\nSending to Gemini gatekeeper...")
    decision = go_live_gatekeeper(metrics)
    print("\nDecision preview:")
    print(decision[:500])
    print("\nFull decision sent via ntfy.")


if __name__ == '__main__':
    main()
