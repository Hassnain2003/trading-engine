"""Cost-aware strategy test of pattern P1 (daily momentum continuation) from
backtest/pattern_mining.py.

Rule (fixed, no fitted parameters): at each UTC midnight, hold the direction
of the previous day's return for the next 24h. A position flip (or fresh
entry) costs one IOC round trip; carrying an unchanged position costs nothing
extra (funding ignored — roughly symmetric long/short over time).

Since the rule has no tunable parameters, the honest score is simply the
VALIDATION window's cost-aware result (the rule was discovered on the
discovery window). Variants: both-directions, long-only, short-only.
"""

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np
import pandas as pd

from backtest._common import fetch_ohlcv_cached, SLIPPAGE, FEE

DAYS = 365
SYMBOLS = [
    "BTC/USDT:USDT", "ETH/USDT:USDT", "SOL/USDT:USDT", "BNB/USDT:USDT",
    "XRP/USDT:USDT", "LINK/USDT:USDT", "DOGE/USDT:USDT",
]
DISCOVERY_FRAC = 2 / 3
FLIP_COST = 2 * (SLIPPAGE + FEE)  # close + open, both crossing spread


def run_symbol(symbol, mode):
    df = fetch_ohlcv_cached(symbol, '1h', DAYS)
    if df.empty:
        return None, None
    d = df.resample('1D').agg({'close': 'last'}).dropna()
    d['ret'] = d['close'].pct_change()

    sig = np.sign(d['ret'].shift(1))
    if mode == 'long_only':
        sig = sig.where(sig > 0, 0)
    elif mode == 'short_only':
        sig = sig.where(sig < 0, 0)
    sig = sig.fillna(0)

    flips = (sig != sig.shift(1)).astype(float) * (sig != 0).astype(float)
    # A flip from +1 to -1 is one round trip (close old, open new); entering
    # from flat is also one round trip's worth over the trade's life.
    pnl = sig * d['ret'] - flips * FLIP_COST

    cut = d.index[int(len(d) * DISCOVERY_FRAC)]
    return pnl[d.index < cut], pnl[d.index >= cut]


def summarize(pnls, label):
    pnl = pd.concat(pnls).dropna()
    active = pnl[pnl != 0]
    if len(active) < 10:
        print(f"  {label}: insufficient data")
        return
    total = (1 + pnl).prod() - 1
    ann_sharpe = pnl.mean() / (pnl.std() + 1e-12) * np.sqrt(365)
    wins = active[active > 0]
    losses = active[active <= 0]
    pf = wins.sum() / abs(losses.sum()) if len(losses) else float('inf')
    equity = (1 + pnl).cumprod()
    dd = ((equity - equity.cummax()) / equity.cummax()).min() * 100
    print(
        f"  {label}: days={len(active)}  PF={pf:.2f}  Sharpe={ann_sharpe:.2f}  "
        f"total={total*100:+.1f}%  MaxDD={dd:.1f}%  avg={pnl.mean()*1e4:+.1f} bps/day"
    )


if __name__ == "__main__":
    for mode in ['both', 'long_only', 'short_only']:
        print(f"\n=== P1 daily momentum, mode={mode} (cost {FLIP_COST*1e4:.0f} bps/flip) ===")
        disc_all, val_all = [], []
        for sym in SYMBOLS:
            disc, val = run_symbol(sym, mode)
            if disc is not None:
                disc_all.append(disc)
                val_all.append(val)
        # per-symbol pooled: average the 7 symbols' daily pnl (equal-weight portfolio)
        disc_port = pd.concat(disc_all, axis=1).mean(axis=1)
        val_port = pd.concat(val_all, axis=1).mean(axis=1)
        summarize([disc_port], "DISCOVERY  (7-symbol equal-weight)")
        summarize([val_port], "VALIDATION (7-symbol equal-weight)")
