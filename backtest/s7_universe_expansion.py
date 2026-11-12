"""S7 universe expansion — which additional liquid perps carry the daily
momentum edge?

The way to a busier winning book is BREADTH on the proven daily strategy,
not intraday frequency (7 intraday attempts have failed OOS). S7 is always-in
and flips daily, so each qualifying coin adds ~1 trade/day, all carrying the
validated edge.

Test: the exact S7 rule (hold prior UTC-day direction, flip on sign change)
with IOC round-trip costs, across a broad candidate universe, discovery/
validation split. A coin qualifies only if PF > 1.05 in BOTH windows with a
reasonable trade count — the same bar the original 7 passed.
"""

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np
import pandas as pd

from backtest._common import fetch_ohlcv_cached, SLIPPAGE, FEE

DAYS = 365
DISCOVERY_FRAC = 2 / 3
FLIP_COST = 2 * (SLIPPAGE + FEE)

INCUMBENTS = ["BTC/USDT:USDT", "ETH/USDT:USDT", "SOL/USDT:USDT", "BNB/USDT:USDT",
              "XRP/USDT:USDT", "LINK/USDT:USDT", "DOGE/USDT:USDT"]
CANDIDATES = [
    "AVAX/USDT:USDT", "ADA/USDT:USDT", "DOT/USDT:USDT", "LTC/USDT:USDT",
    "ATOM/USDT:USDT", "NEAR/USDT:USDT", "APT/USDT:USDT", "ARB/USDT:USDT",
    "OP/USDT:USDT", "INJ/USDT:USDT", "SUI/USDT:USDT", "TIA/USDT:USDT",
    "TRX/USDT:USDT", "UNI/USDT:USDT", "AAVE/USDT:USDT", "FIL/USDT:USDT",
    "ICP/USDT:USDT", "ETC/USDT:USDT", "XLM/USDT:USDT", "HBAR/USDT:USDT",
]


def s7_pnl(symbol):
    try:
        df = fetch_ohlcv_cached(symbol, '1d', DAYS)
    except Exception as e:
        return None
    if df.empty or len(df) < 200:
        return None
    close = df['close']
    ret = close.pct_change()
    sig = np.sign(ret.shift(1)).fillna(0)
    flips = (sig != sig.shift(1)).astype(float) * (sig != 0).astype(float)
    return (sig * ret - flips * FLIP_COST).dropna()


def evaluate(symbol):
    pnl = s7_pnl(symbol)
    if pnl is None or len(pnl) < 150:
        return None
    cut = pnl.index[int(len(pnl) * DISCOVERY_FRAC)]
    out = {}
    for name, seg in [('disc', pnl[pnl.index < cut]), ('val', pnl[pnl.index >= cut])]:
        active = seg[seg != 0]
        if len(active) < 20:
            return None
        wins = active[active > 0]; losses = active[active <= 0]
        pf = wins.sum() / abs(losses.sum()) if len(losses) else 99.0
        out[name] = {'pf': pf, 'n': len(active), 'wr': len(wins) / len(active) * 100,
                     'avg_bps': seg.mean() * 1e4}
    return out


if __name__ == "__main__":
    print("=== Incumbents (reference) ===")
    for sym in INCUMBENTS:
        r = evaluate(sym)
        if r:
            print(f"  {sym.split('/')[0]:6s} disc PF {r['disc']['pf']:.2f} | val PF {r['val']['pf']:.2f} "
                  f"(val {r['val']['avg_bps']:+.1f} bps/day)")

    print("\n=== Candidates ===")
    qualified = []
    for sym in CANDIDATES:
        print(f"  testing {sym.split('/')[0]}...", flush=True)
        r = evaluate(sym)
        if r is None:
            print(f"    {sym.split('/')[0]}: insufficient data/history — skip")
            continue
        passed = r['disc']['pf'] > 1.05 and r['val']['pf'] > 1.05
        tag = "✅ QUALIFIES" if passed else "✗"
        print(f"    {sym.split('/')[0]:6s} disc PF {r['disc']['pf']:.2f} | val PF {r['val']['pf']:.2f} "
              f"(val {r['val']['avg_bps']:+.1f} bps/day, WR {r['val']['wr']:.0f}%)  {tag}")
        if passed:
            qualified.append((sym, r))

    print("\n========== QUALIFYING NEW COINS (positive both windows) ==========")
    if not qualified:
        print("  none passed — keep the current set")
    for sym, r in sorted(qualified, key=lambda x: x[1]['val']['pf'], reverse=True):
        print(f"  {sym}: val PF {r['val']['pf']:.2f}, {r['val']['avg_bps']:+.1f} bps/day")
