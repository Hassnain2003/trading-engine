"""Cross-sectional momentum study (Phase 2 research).

Different edge family from S7 (time-series momentum): instead of asking "did
this coin go up yesterday?", rank the 7 coins against each other by trailing
return and go long the strongest / short the weakest. Partially market-
neutral, so it can pay in flat markets where S7 struggles.

Grid (small, pre-registered): lookback in {1, 3, 7} days, top/bottom 1 or 2
coins per side, daily rebalance at UTC midnight. Costs: a leg pays one IOC
round trip (~14 bps) only when its coin changes.

Same discovery/validation discipline as pattern_mining.py.
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
COST_PER_CHANGE = 2 * (SLIPPAGE + FEE)  # round trip when a leg rotates

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


def run_config(px, lookback, n_side):
    rets = px.pct_change()
    signal_rank = px.pct_change(lookback).shift(1)  # known at day start
    daily_pnl = []
    prev_long, prev_short = set(), set()

    for day in px.index[lookback + 1:]:
        row = signal_rank.loc[day].dropna()
        if len(row) < 4:
            continue
        ranked = row.sort_values()
        shorts = set(ranked.index[:n_side])
        longs = set(ranked.index[-n_side:])

        day_ret = rets.loc[day]
        gross = (day_ret[list(longs)].mean() - day_ret[list(shorts)].mean()) / 2
        changes = len(longs - prev_long) + len(shorts - prev_short)
        cost = changes * COST_PER_CHANGE / (2 * n_side)
        daily_pnl.append(gross - cost)
        prev_long, prev_short = longs, shorts

    return pd.Series(daily_pnl, index=px.index[lookback + 1:][:len(daily_pnl)])


def summarize(pnl, label):
    if len(pnl) < 30:
        print(f"  {label}: insufficient data")
        return
    wins = pnl[pnl > 0]
    losses = pnl[pnl <= 0]
    pf = wins.sum() / abs(losses.sum()) if len(losses) else float('inf')
    sharpe = pnl.mean() / (pnl.std() + 1e-12) * np.sqrt(365)
    total = ((1 + pnl).prod() - 1) * 100
    print(
        f"  {label}: days={len(pnl)}  PF={pf:.2f}  Sharpe={sharpe:+.2f}  "
        f"total={total:+.1f}%  avg={pnl.mean()*1e4:+.1f} bps/day"
    )


if __name__ == "__main__":
    px = daily_closes()
    cut = int(len(px) * DISCOVERY_FRAC)

    for lb in LOOKBACKS:
        for ns in N_PER_SIDE:
            pnl = run_config(px, lb, ns)
            cut_ts = px.index[cut]
            print(f"\nXS momentum lookback={lb}d, {ns}/side:")
            summarize(pnl[pnl.index < cut_ts], "DISCOVERY ")
            summarize(pnl[pnl.index >= cut_ts], "VALIDATION")
