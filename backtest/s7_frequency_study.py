"""Does momentum continuation work at intraday rebalance frequencies?

Prompted by the user (2026-07-11): why one daily verdict — why not 4-5/day?
Test: the S7 rule (hold the direction of the previous period) at rebalance
periods of 4h/8h/12h vs the validated 24h, with flip costs charged whenever
the sign changes. Same discovery/validation split as pattern_mining.py.

More verdicts = more information updates but also more cost-bearing flips;
this measures whether the shorter-horizon signal is strong enough to pay
its own churn.
"""

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np
import pandas as pd

from backtest._common import fetch_ohlcv_cached, SLIPPAGE, FEE

SYMBOLS = [
    "BTC/USDT:USDT", "ETH/USDT:USDT", "SOL/USDT:USDT", "BNB/USDT:USDT",
    "XRP/USDT:USDT", "LINK/USDT:USDT", "DOGE/USDT:USDT",
]
DAYS = 365
DISCOVERY_FRAC = 2 / 3
FLIP_COST = 2 * (SLIPPAGE + FEE)
PERIODS = ['4h', '8h', '12h', '24h']


def run_symbol(symbol, period):
    h1 = fetch_ohlcv_cached(symbol, '1h', DAYS)
    if h1.empty:
        return None
    px = h1['close'].resample(period).last().dropna()
    ret = px.pct_change()
    sig = np.sign(ret.shift(1)).fillna(0)
    flips = (sig != sig.shift(1)).astype(float) * (sig != 0).astype(float)
    return sig * ret - flips * FLIP_COST


def summarize(pnl, label, periods_per_year):
    pnl = pnl.dropna()
    if len(pnl) < 30:
        print(f"  {label}: insufficient data")
        return
    active = pnl[pnl != 0]
    wins = active[active > 0]
    losses = active[active <= 0]
    pf = wins.sum() / abs(losses.sum()) if len(losses) else float('inf')
    sharpe = pnl.mean() / (pnl.std() + 1e-12) * np.sqrt(periods_per_year)
    total = ((1 + pnl).prod() - 1) * 100
    daily_bps = pnl.mean() * 1e4 * periods_per_year / 365
    print(f"  {label}: PF={pf:.2f}  Sharpe={sharpe:+.2f}  total={total:+.1f}%  avg={daily_bps:+.1f} bps/day-equivalent")


if __name__ == "__main__":
    for period in PERIODS:
        hours = int(period.replace('h', ''))
        ppy = 365 * 24 / hours
        print(f"\n=== Rebalance every {period} ({int(24/hours*10)/10 if hours<24 else 1} verdicts/day) ===")
        per_symbol = []
        for sym in SYMBOLS:
            s = run_symbol(sym, period)
            if s is not None:
                per_symbol.append(s)
        port = pd.concat(per_symbol, axis=1).mean(axis=1)
        cut = port.index[int(len(port) * DISCOVERY_FRAC)]
        summarize(port[port.index < cut], "DISCOVERY ", ppy)
        summarize(port[port.index >= cut], "VALIDATION", ppy)
