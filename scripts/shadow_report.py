#!/usr/bin/env python3
"""Score the S8 shadow signals: do Gemini's news calls predict price?

    python scripts/shadow_report.py
"""

import os
import sys
import sqlite3

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from trading_phase import DB_FILENAME
DB = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), DB_FILENAME)

if __name__ == "__main__":
    conn = sqlite3.connect(DB)
    n_total = conn.execute("SELECT COUNT(*) FROM shadow_signals").fetchone()[0]
    print(f"Shadow signals recorded: {n_total}")

    for col, label in [('price_1h', '1h'), ('price_4h', '4h'), ('price_24h', '24h')]:
        rows = conn.execute(
            f"""SELECT direction, severity, price_at_signal, {col}
                FROM shadow_signals
                WHERE {col} IS NOT NULL AND price_at_signal IS NOT NULL
                AND direction IN ('bullish', 'bearish')"""
        ).fetchall()
        if not rows:
            print(f"\n[{label}] no matured directional signals yet")
            continue
        correct, total, ret_sum = 0, 0, 0.0
        hi_correct, hi_total = 0, 0
        for direction, severity, p0, p1 in rows:
            ret = (p1 - p0) / p0 * (1 if direction == 'bullish' else -1)
            ret_sum += ret
            total += 1
            correct += ret > 0
            if severity and severity >= 3:
                hi_total += 1
                hi_correct += ret > 0
        print(f"\n[{label}] directional signals: {total}")
        print(f"  hit rate: {correct/total*100:.1f}%  avg signed ret: {ret_sum/total*1e4:+.1f} bps")
        if hi_total:
            print(f"  severity>=3 only: {hi_total} signals, hit rate {hi_correct/hi_total*100:.1f}%")

    print("\nBy direction (all matured 24h):")
    for d in ('bullish', 'bearish', 'neutral'):
        n = conn.execute("SELECT COUNT(*) FROM shadow_signals WHERE direction=?", (d,)).fetchone()[0]
        print(f"  {d}: {n}")
