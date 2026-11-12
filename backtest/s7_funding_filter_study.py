"""S7 squeeze-filter pre-validation.

funding_study.py found that deeply negative funding (crowded shorts) precedes
price RISES in both windows (+4.9 / +18.8 bps per 8h) — short-squeeze fuel.
S7 holds shorts on down days, which is exactly when funding tends negative,
so the question: does SKIPPING S7 short-days when funding is deeply negative
improve the strategy?

Pre-registered threshold from funding_study.py: funding < -0.01% (-0.0001)
on the last 8h settlement before the day starts. No threshold search — one
hypothesis, tested once, in both windows.
"""

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np
import pandas as pd

from backtest._common import fetch_ohlcv_cached, SLIPPAGE, FEE
from backtest.funding_study import fetch_funding_cached

SYMBOLS = [
    "BTC/USDT:USDT", "ETH/USDT:USDT", "SOL/USDT:USDT", "BNB/USDT:USDT",
    "XRP/USDT:USDT", "LINK/USDT:USDT", "DOGE/USDT:USDT",
]
DAYS = 365
DISCOVERY_FRAC = 2 / 3
FLIP_COST = 2 * (SLIPPAGE + FEE)
SQUEEZE_THRESHOLD = -0.0001  # pre-registered in funding_study.py


def run_symbol(symbol, use_filter):
    h1 = fetch_ohlcv_cached(symbol, '1h', DAYS)
    funding = fetch_funding_cached(symbol)
    if h1.empty or funding.empty:
        return None
    d = h1.resample('1D').agg({'close': 'last'}).dropna()
    d['ret'] = d['close'].pct_change()

    # Last funding rate settled before each day starts
    last_funding = funding['rate'].resample('1D').last().reindex(d.index).ffill().shift(1)

    sig = np.sign(d['ret'].shift(1)).fillna(0)
    if use_filter:
        squeeze_risk = (sig < 0) & (last_funding < SQUEEZE_THRESHOLD)
        sig = sig.where(~squeeze_risk, 0)  # stand flat instead of short

    flips = (sig != sig.shift(1)).astype(float) * (sig != 0).astype(float)
    pnl = sig * d['ret'] - flips * FLIP_COST
    return pnl


def summarize(pnl, label):
    pnl = pnl.dropna()
    active = pnl[pnl != 0]
    if len(active) < 30:
        print(f"  {label}: insufficient data")
        return
    wins = active[active > 0]
    losses = active[active <= 0]
    pf = wins.sum() / abs(losses.sum()) if len(losses) else float('inf')
    sharpe = pnl.mean() / (pnl.std() + 1e-12) * np.sqrt(365)
    total = ((1 + pnl).prod() - 1) * 100
    print(f"  {label}: PF={pf:.2f}  Sharpe={sharpe:+.2f}  total={total:+.1f}%  avg={pnl.mean()*1e4:+.1f} bps/day")


if __name__ == "__main__":
    for use_filter, name in [(False, "BASELINE (no filter)"), (True, f"SQUEEZE FILTER (skip shorts when funding < {SQUEEZE_THRESHOLD:.4%})")]:
        print(f"\n=== {name} ===")
        per_symbol = []
        for sym in SYMBOLS:
            s = run_symbol(sym, use_filter)
            if s is not None:
                per_symbol.append(s)
        port = pd.concat(per_symbol, axis=1).mean(axis=1)
        cut = port.index[int(len(port) * DISCOVERY_FRAC)]
        summarize(port[port.index < cut], "DISCOVERY ")
        summarize(port[port.index >= cut], "VALIDATION")
