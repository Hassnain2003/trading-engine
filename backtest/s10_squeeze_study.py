"""S10 candidate — intraday Volatility Squeeze Breakout (15m).

Goal (user request 2026-07-14): a genuinely all-day, event-driven strategy
that trades whenever the market sets up, not on a daily clock — to complement
S2 (daily-range breakout) and S7 (daily momentum).

Thesis: volatility is mean-reverting. When Bollinger Bands contract inside the
Keltner Channel ("the squeeze"), price is coiling; the release tends to expand
directionally. Enter on the squeeze release in the direction of the break of
the recent N-bar high/low. ATR stop, R-multiple target. This can fire at any
hour, multiple times a day, as often as the market actually coils — exactly
the "whole day" behavior asked for.

Grid: BB/KC length 20, KC ATR mult {1.5, 2.0}, breakout lookback {10, 20},
stop ATR mult {1.5, 2.0}, target R {1.5, 2.0, 3.0}. 7 symbols, 180d, 15m.
Discovery = first 2/3, validation = last 1/3 (never touched in tuning).
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

BB_LEN = 20
KC_MULTS = [1.5, 2.0]
BREAK_LOOKBACKS = [10, 20]
STOP_ATR_MULTS = [1.5, 2.0]
TARGET_RS = [1.5, 2.0, 3.0]


def build_frame(symbol):
    df = fetch_ohlcv_cached(symbol, '15m', DAYS)
    if df.empty or len(df) < 1000:
        return None
    close, high, low = df['close'], df['high'], df['low']
    basis = close.rolling(BB_LEN).mean()
    dev = close.rolling(BB_LEN).std()
    atr = ta.atr(high, low, close, length=BB_LEN)
    df['_basis'] = basis
    df['_bb_dev'] = dev
    df['_atr'] = atr
    return df


def run_config(df, kc_mult, lookback, stop_mult, target_r):
    close, high, low = df['close'], df['high'], df['low']
    basis, dev, atr = df['_basis'], df['_bb_dev'], df['_atr']

    bb_upper = basis + 2 * dev
    bb_lower = basis - 2 * dev
    kc_upper = basis + kc_mult * atr
    kc_lower = basis - kc_mult * atr

    squeeze_on = (bb_upper < kc_upper) & (bb_lower > kc_lower)
    released = squeeze_on.shift(1).fillna(False) & (~squeeze_on.fillna(False))

    hh = high.rolling(lookback).max().shift(1)
    ll = low.rolling(lookback).min().shift(1)

    entries_long = (released & (close > hh) & atr.notna()).fillna(False)
    entries_short = (released & (close < ll) & atr.notna()).fillna(False)

    sl_long = close - stop_mult * atr
    tp_long = close + target_r * stop_mult * atr
    sl_short = close + stop_mult * atr
    tp_short = close - target_r * stop_mult * atr

    return simulate_trades(df, entries_long, entries_short, sl_long, tp_long, sl_short, tp_short, "")


if __name__ == "__main__":
    frames = {}
    for sym in SYMBOLS:
        print(f"  loading {sym}...")
        frames[sym] = build_frame(sym)

    results = []
    for kc in KC_MULTS:
        for lb in BREAK_LOOKBACKS:
            for sm in STOP_ATR_MULTS:
                for tr in TARGET_RS:
                    disc_trades, val_trades = [], []
                    for sym, df in frames.items():
                        if df is None:
                            continue
                        t = run_config(df, kc, lb, sm, tr)
                        if t.empty:
                            continue
                        t = t.sort_values('entry_time')
                        cut = int(len(t) * DISCOVERY_FRAC)
                        disc_trades.append(t.iloc[:cut])
                        val_trades.append(t.iloc[cut:])
                    if not disc_trades:
                        continue
                    disc = pd.concat(disc_trades)
                    val = pd.concat(val_trades)
                    label = f"KC{kc} LB{lb} SL{sm} TP{tr}R"
                    dr = summarize(disc, f"  [D] {label}")
                    vr = summarize(val, f"  [V] {label}")
                    if dr['trades'] and vr['trades']:
                        results.append((label, dr, vr))

    print("\n========== RANKED: configs POSITIVE in BOTH windows ==========")
    good = [(lbl, d, v) for lbl_dv in results for (lbl, d, v) in [lbl_dv]
            if d['pf'] and v['pf'] and d['pf'] > 1.1 and v['pf'] > 1.1]
    good.sort(key=lambda x: x[2]['pf'], reverse=True)
    if not good:
        print("  NONE — no squeeze config held up out-of-sample. Thesis fails the gate.")
    for lbl, d, v in good:
        print(f"  {lbl}: discovery PF {d['pf']:.2f} ({d['trades']}tr) | "
              f"validation PF {v['pf']:.2f} ({v['trades']}tr, WR {v['win_rate']:.0f}%)")
