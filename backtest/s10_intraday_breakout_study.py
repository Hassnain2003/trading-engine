"""S10 candidate v2 — Intraday Rolling-Range Breakout-Retest (15m).

The squeeze thesis (s10_squeeze_study.py) failed OOS. This tries the smarter
move: take S2's PROVEN logic (breakout of a range + volume spike + retest +
trend alignment — validated live at PF 1.37) and apply it to a ROLLING
intraday range (prior K bars) instead of the single daily opening range, so
it can fire multiple times a day at any hour.

Entry long: close breaks above the highest high of the prior RANGE_BARS bars,
with volume > VOL_MULT * avg, price above the trend EMA, then a retest holds.
Short is the mirror. ATR stop, R-multiple target. Grid + discovery/validation.
"""

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np
import pandas as pd
import pandas_ta as ta

from backtest._common import fetch_ohlcv_cached, simulate_trades, summarize

SYMBOLS = [
    "BTC/USDT:USDT", "ETH/USDT:USDT", "SOL/USDT:USDT", "BNB/USDT:USDT",
    "XRP/USDT:USDT", "LINK/USDT:USDT", "DOGE/USDT:USDT",
]
DAYS = 180
DISCOVERY_FRAC = 2 / 3

RANGE_BARS_GRID = [16, 32]        # 4h / 8h rolling range on 15m bars
VOL_MULTS = [1.5, 2.0]
TREND_EMA = 200
STOP_ATR_MULTS = [1.5, 2.0]
TARGET_RS = [1.5, 2.0]


def build_frame(symbol):
    df = fetch_ohlcv_cached(symbol, '15m', DAYS)
    if df.empty or len(df) < 1000:
        return None
    df['_atr'] = ta.atr(df['high'], df['low'], df['close'], length=14)
    df['_ema'] = ta.ema(df['close'], length=TREND_EMA)
    df['_avgvol'] = df['volume'].shift(1).rolling(20).mean()
    return df


def run_config(df, range_bars, vol_mult, stop_mult, target_r):
    close, high, low, vol = df['close'], df['high'], df['low'], df['volume']
    atr, ema, avgvol = df['_atr'], df['_ema'], df['_avgvol']

    range_hi = high.rolling(range_bars).max().shift(1)
    range_lo = low.rolling(range_bars).min().shift(1)
    vol_ok = vol >= vol_mult * avgvol

    broke_up = (close > range_hi) & vol_ok & (close > ema)
    broke_dn = (close < range_lo) & vol_ok & (close < ema)

    # Retest: prior bar broke out, current bar pulls back toward the level
    # but still holds beyond it (the entry bar).
    retest_long = broke_up.shift(1).fillna(False) & (close > range_hi.shift(1)) & (low <= range_hi.shift(1) * 1.001)
    retest_short = broke_dn.shift(1).fillna(False) & (close < range_lo.shift(1)) & (high >= range_lo.shift(1) * 0.999)

    entries_long = (retest_long & atr.notna()).fillna(False)
    entries_short = (retest_short & atr.notna()).fillna(False)

    sl_long = close - stop_mult * atr
    tp_long = close + target_r * stop_mult * atr
    sl_short = close + stop_mult * atr
    tp_short = close - target_r * stop_mult * atr

    return simulate_trades(df, entries_long, entries_short, sl_long, tp_long, sl_short, tp_short, "")


if __name__ == "__main__":
    frames = {sym: build_frame(sym) for sym in SYMBOLS}

    results = []
    for rb in RANGE_BARS_GRID:
        for vm in VOL_MULTS:
            for sm in STOP_ATR_MULTS:
                for tr in TARGET_RS:
                    disc_t, val_t = [], []
                    for sym, df in frames.items():
                        if df is None:
                            continue
                        t = run_config(df, rb, vm, sm, tr)
                        if t.empty:
                            continue
                        t = t.sort_values('entry_time')
                        cut = int(len(t) * DISCOVERY_FRAC)
                        disc_t.append(t.iloc[:cut]); val_t.append(t.iloc[cut:])
                    if not disc_t:
                        continue
                    disc, val = pd.concat(disc_t), pd.concat(val_t)
                    label = f"RB{rb} VOL{vm} SL{sm} TP{tr}R"
                    dr = summarize(disc, f"  [D] {label}")
                    vr = summarize(val, f"  [V] {label}")
                    if dr['trades'] and vr['trades']:
                        results.append((label, dr, vr))

    print("\n========== RANKED: positive in BOTH windows ==========")
    good = [(lbl, d, v) for (lbl, d, v) in results
            if d['pf'] and v['pf'] and d['pf'] > 1.1 and v['pf'] > 1.1]
    good.sort(key=lambda x: x[2]['pf'], reverse=True)
    if not good:
        print("  NONE — intraday breakout-retest does not survive OOS either.")
    for lbl, d, v in good:
        tot = d['trades'] + v['trades']
        print(f"  {lbl}: disc PF {d['pf']:.2f} | val PF {v['pf']:.2f} "
              f"(val WR {v['win_rate']:.0f}%, {tot} total trades ~{tot/180:.1f}/day)")
