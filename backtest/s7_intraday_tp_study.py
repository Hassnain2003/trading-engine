"""Does intraday profit-taking improve S7 (daily momentum), or does it cut
the winners that pay for the strategy?

Prompted by live observation 2026-07-09: an ETH short went +$24 intraday and
round-tripped to ~0 by the next check. Hypothesis to test: exit intraday when
profit reaches K × daily ATR instead of always holding to the day boundary.

Simulation on 1h bars: each day, position = sign(prev day's return), entered
at the day's first 1h open. Exit at intraday TP (close-basis on 1h bars) if
hit, else at the day's last close. Costs: entering a day whose direction
equals the previous day's *and* which wasn't TP-exited yesterday is a
continuation (no cost); any TP exit forces a re-entry cost next day; flips
cost one round trip as usual. Same discovery/validation split as always.
"""

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np
import pandas as pd
import pandas_ta as ta

from backtest._common import fetch_ohlcv_cached, SLIPPAGE, FEE

SYMBOLS = [
    "BTC/USDT:USDT", "ETH/USDT:USDT", "SOL/USDT:USDT", "BNB/USDT:USDT",
    "XRP/USDT:USDT", "LINK/USDT:USDT", "DOGE/USDT:USDT",
]
DAYS = 365
DISCOVERY_FRAC = 2 / 3
RT_COST = 2 * (SLIPPAGE + FEE)   # round trip
TP_ATR_MULTS = [None, 0.5, 1.0, 1.5]   # None = pure daily hold (baseline)


def run_symbol(symbol, tp_mult):
    h1 = fetch_ohlcv_cached(symbol, '1h', DAYS)
    if h1.empty:
        return None
    d = h1.resample('1D').agg({'open': 'first', 'high': 'max', 'low': 'min', 'close': 'last'}).dropna()
    d['ret'] = d['close'].pct_change()
    atr = ta.atr(d['high'], d['low'], d['close'], length=14)

    daily_pnl = []
    prev_dir = 0
    prev_tp_exited = False
    for i in range(15, len(d) - 1):
        day = d.index[i]
        direction = np.sign(d['ret'].iloc[i - 1])
        if direction == 0 or np.isnan(atr.iloc[i - 1]):
            daily_pnl.append((day, 0.0))
            prev_dir = 0
            continue

        bars = h1[(h1.index >= day) & (h1.index < day + pd.Timedelta(days=1))]
        if len(bars) < 4:
            daily_pnl.append((day, 0.0))
            continue

        entry = bars['open'].iloc[0]
        tp_price = None
        if tp_mult is not None:
            tp_price = entry + direction * tp_mult * atr.iloc[i - 1]

        exit_price = bars['close'].iloc[-1]
        tp_exited = False
        if tp_price is not None:
            for _, bar in bars.iterrows():
                if (direction > 0 and bar['high'] >= tp_price) or (direction < 0 and bar['low'] <= tp_price):
                    exit_price = tp_price
                    tp_exited = True
                    break

        gross = direction * (exit_price - entry) / entry
        # Cost: pay a round trip if we had to (re)enter today — i.e. direction
        # changed from yesterday, or yesterday's position TP-exited.
        cost = RT_COST if (direction != prev_dir or prev_tp_exited) else 0.0
        # TP exit itself pays the closing half of a round trip early; fold it
        # into next day's re-entry cost (handled above) — no double count.
        daily_pnl.append((day, gross - cost))
        prev_dir = direction
        prev_tp_exited = tp_exited

    s = pd.Series(dict(daily_pnl))
    return s


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
    for tp in TP_ATR_MULTS:
        name = "HOLD to day end (baseline)" if tp is None else f"TP at {tp}x daily ATR"
        print(f"\n=== {name} ===")
        per_symbol = []
        for sym in SYMBOLS:
            s = run_symbol(sym, tp)
            if s is not None:
                per_symbol.append(s)
        port = pd.concat(per_symbol, axis=1).mean(axis=1)
        cut = port.index[int(len(port) * DISCOVERY_FRAC)]
        summarize(port[port.index < cut], "DISCOVERY ")
        summarize(port[port.index >= cut], "VALIDATION")
