"""S2 breadth test: does the validated S2 edge (opening-range breakout-retest,
2.0R target — see backtest/s2_r_multiple_walkforward.py, OOS PF 1.17 on
SOL+BNB) generalize to the other actively-traded symbols?

Reuses the exact same signal construction as relaxed_variants.py's
backtest_s2_wider_target (which matches the live strategy code) at R=2.0,
over 6 months per symbol. Symbols that look positive here get walk-forward
checked before being added to the live COINS list.
"""

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd

from backtest._common import summarize
from backtest.relaxed_variants import backtest_s2_wider_target
from strategies.strategy2_breakout_retest import TP_R_MULTIPLE

CANDIDATES = ["BTC/USDT:USDT", "ETH/USDT:USDT", "XRP/USDT:USDT", "LINK/USDT:USDT", "DOGE/USDT:USDT"]
INCUMBENTS = ["SOL/USDT:USDT", "BNB/USDT:USDT"]

if __name__ == "__main__":
    print(f"=== S2 breadth test @ {TP_R_MULTIPLE}R, 180 days ===\n")
    print("--- incumbents (baseline reference) ---")
    for sym in INCUMBENTS:
        t = backtest_s2_wider_target(sym, TP_R_MULTIPLE)
        if t is not None and not t.empty:
            summarize(t, f"  S2 {sym}")

    print("\n--- candidates ---")
    results = []
    for sym in CANDIDATES:
        t = backtest_s2_wider_target(sym, TP_R_MULTIPLE)
        if t is not None and not t.empty:
            r = summarize(t, f"  S2 {sym}")
            r['symbol'] = sym
            results.append(r)
        else:
            print(f"  S2 {sym}: no trades / no data")

    promising = [r for r in results if r['pf'] and r['pf'] > 1.0]
    print("\n=== Candidates with in-sample PF > 1.0 (need walk-forward before going live) ===")
    for r in promising:
        print(f"  {r['symbol']}  PF={r['pf']:.2f}  trades={r['trades']}  WR={r['win_rate']:.1f}%")
    if not promising:
        print("  none — S2's edge does not generalize beyond SOL/BNB at these settings")
