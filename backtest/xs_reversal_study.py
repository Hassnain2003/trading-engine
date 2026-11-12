"""Cross-sectional REVERSAL study — follow-up to xs_momentum_study.py.

That study's momentum configs lost consistently in BOTH windows (Sharpe -0.8
to -2.0), which for a symmetric long-short book implies the mirror trade —
long the relative losers, short the relative winners — has positive gross
edge. This tests the reversal direction with costs accounted properly
(costs are paid on rotation regardless of direction; they don't flip sign).

Reports gross and net separately so the cost drag is visible.
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
COST_PER_CHANGE = 2 * (SLIPPAGE + FEE)

LOOKBACKS = [1, 3, 7]
N_PER_SIDE = [1, 2]


def daily_closes():
    cols = {}
    for sym in SYMBOLS:
        df = fetch_ohlcv_cached(sym, '1h', DAYS)
        if df.empty:
            continue
        cols[sym.split('/')[0]] = df['close'].resample('1D').last()
    return pd.DataFrame(cols).dropna()


def run_reversal(px, lookback, n_side):
    rets = px.pct_change()
    signal_rank = px.pct_change(lookback).shift(1)
    gross_l, cost_l = [], []
    prev_long, prev_short = set(), set()

    for day in px.index[lookback + 1:]:
        row = signal_rank.loc[day].dropna()
        if len(row) < 4:
            continue
        ranked = row.sort_values()
        longs = set(ranked.index[:n_side])       # relative LOSERS → long
        shorts = set(ranked.index[-n_side:])     # relative WINNERS → short

        day_ret = rets.loc[day]
        gross = (day_ret[list(longs)].mean() - day_ret[list(shorts)].mean()) / 2
        changes = len(longs - prev_long) + len(shorts - prev_short)
        cost = changes * COST_PER_CHANGE / (2 * n_side)
        gross_l.append(gross)
        cost_l.append(cost)
        prev_long, prev_short = longs, shorts

    idx = px.index[lookback + 1:][:len(gross_l)]
    return pd.Series(gross_l, index=idx), pd.Series(cost_l, index=idx)


def summarize(gross, cost, label):
    net = gross - cost
    if len(net) < 30:
        print(f"  {label}: insufficient data")
        return
    wins = net[net > 0]
    losses = net[net <= 0]
    pf = wins.sum() / abs(losses.sum()) if len(losses) else float('inf')
    sharpe = net.mean() / (net.std() + 1e-12) * np.sqrt(365)
    total = ((1 + net).prod() - 1) * 100
    print(
        f"  {label}: days={len(net)}  gross={gross.mean()*1e4:+.1f} cost={cost.mean()*1e4:.1f} "
        f"net={net.mean()*1e4:+.1f} bps/day  PF={pf:.2f}  Sharpe={sharpe:+.2f}  total={total:+.1f}%"
    )


if __name__ == "__main__":
    px = daily_closes()
    cut = int(len(px) * DISCOVERY_FRAC)
    cut_ts = px.index[cut]

    for lb in LOOKBACKS:
        for ns in N_PER_SIDE:
            gross, cost = run_reversal(px, lb, ns)
            print(f"\nXS REVERSAL lookback={lb}d, {ns}/side (long losers / short winners):")
            summarize(gross[gross.index < cut_ts], cost[cost.index < cut_ts], "DISCOVERY ")
            summarize(gross[gross.index >= cut_ts], cost[cost.index >= cut_ts], "VALIDATION")
