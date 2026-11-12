"""Follow-up experiments on top of relaxed_current_params.py's baseline
results (S2 PF 0.96, S3 PF 0.34, S4 PF 0.74 — see that file's docstring for
methodology/simplifications). Reuses the same cached OHLCV data.

Variant A — S4 divergence-only: drops the "RSI-extreme + MACD-cross" fallback
that was bolted on to strategy4_rsi_macd.py for demo signal frequency, to see
if it's diluting the divergence signal with lower-quality trades.

Variant B — S2 wider target: tests R-multiples other than the live 1.5x to
see if S2's near-breakeven PF (0.96) flips positive with a bigger target.
"""

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np
import pandas as pd
import pandas_ta as ta

from backtest._common import fetch_ohlcv_cached, simulate_trades, summarize
from strategies.strategy2_breakout_retest import (
    OPENING_MINUTES, VOL_SPIKE_MULT, RETEST_ZONE_PCT, EMA_FAST, EMA_MEDIUM, EMA_SLOW,
)
from strategies.strategy4_rsi_macd import (
    RSI_PERIOD as S4_RSI_PERIOD, MACD_FAST, MACD_SLOW, MACD_SIGNAL, RSI_OS, RSI_OB,
)

DAYS = 180


# ───────────────── Variant A: S4 divergence-only (no RSI/MACD-cross fallback) ─────────────────

def backtest_s4_divergence_only(symbol):
    df = fetch_ohlcv_cached(symbol, '15m', DAYS)
    if df.empty or len(df) < 500:
        return None

    close = df['close']
    rsi = ta.rsi(close, length=S4_RSI_PERIOD)
    macd_df = ta.macd(close, fast=MACD_FAST, slow=MACD_SLOW, signal=MACD_SIGNAL)
    macd_line = macd_df[f'MACD_{MACD_FAST}_{MACD_SLOW}_{MACD_SIGNAL}']
    macd_signal = macd_df[f'MACDs_{MACD_FAST}_{MACD_SLOW}_{MACD_SIGNAL}']
    macd_hist = macd_df[f'MACDh_{MACD_FAST}_{MACD_SLOW}_{MACD_SIGNAL}']

    is_swing_low = (close < close.shift(1)) & (close < close.shift(-1))
    is_swing_high = (close > close.shift(1)) & (close > close.shift(-1))

    n = len(df)
    entries_long = np.zeros(n, dtype=bool)
    entries_short = np.zeros(n, dtype=bool)
    recent_lows, recent_highs = [], []
    WINDOW = 25

    for i in range(n):
        while recent_lows and recent_lows[0] < i - WINDOW:
            recent_lows.pop(0)
        while recent_highs and recent_highs[0] < i - WINDOW:
            recent_highs.pop(0)

        if i < 50:
            if is_swing_low.iloc[i]:
                recent_lows.append(i)
            if is_swing_high.iloc[i]:
                recent_highs.append(i)
            continue

        # NOTE: no RSI-extreme+MACD-cross fallback here — divergence only.
        if len(recent_lows) >= 2:
            i1, i2 = recent_lows[-2], recent_lows[-1]
            if close.iloc[i2] < close.iloc[i1] and rsi.iloc[i2] > rsi.iloc[i1]:
                if (macd_hist.iloc[i] > macd_hist.iloc[i - 1]) or (macd_line.iloc[i] > macd_signal.iloc[i]):
                    entries_long[i] = True
        if len(recent_highs) >= 2:
            i1, i2 = recent_highs[-2], recent_highs[-1]
            if close.iloc[i2] > close.iloc[i1] and rsi.iloc[i2] < rsi.iloc[i1]:
                if (macd_hist.iloc[i] < macd_hist.iloc[i - 1]) or (macd_line.iloc[i] < macd_signal.iloc[i]):
                    entries_short[i] = True

        if is_swing_low.iloc[i]:
            recent_lows.append(i)
        if is_swing_high.iloc[i]:
            recent_highs.append(i)

    entries_long = pd.Series(entries_long, index=df.index)
    entries_short = pd.Series(entries_short, index=df.index)

    swing_low_10 = df['low'].rolling(10).min()
    swing_high_10 = df['high'].rolling(10).max()
    sl_long = swing_low_10
    tp_long = close + 2 * (close - sl_long)
    sl_short = swing_high_10
    tp_short = close - 2 * (sl_short - close)

    return simulate_trades(df, entries_long, entries_short, sl_long, tp_long, sl_short, tp_short, symbol)


# ───────────────── Variant B: S2 with wider profit target ─────────────────

def backtest_s2_wider_target(symbol, r_multiple):
    df = fetch_ohlcv_cached(symbol, '5m', DAYS)
    if df.empty or len(df) < 500:
        return None

    dates = df.index.date
    daily = df.groupby(dates).apply(lambda g: g.iloc[:max(1, OPENING_MINUTES // 5)].agg({'high': 'max', 'low': 'min'}))
    session_high = pd.Series(dates, index=df.index).map(daily['high'])
    session_low = pd.Series(dates, index=df.index).map(daily['low'])

    df_1h = df.resample('1h').agg({'open': 'first', 'high': 'max', 'low': 'min', 'close': 'last', 'volume': 'sum'}).dropna()
    ema_fast = ta.ema(df_1h['close'], length=EMA_FAST)
    ema_medium = ta.ema(df_1h['close'], length=EMA_MEDIUM)
    ema_slow = ta.ema(df_1h['close'], length=EMA_SLOW)
    trend_bullish = ((ema_fast > ema_medium) & (ema_medium > ema_slow)).reindex(df.index, method='ffill').fillna(False)
    trend_bearish = ((ema_fast < ema_medium) & (ema_medium < ema_slow)).reindex(df.index, method='ffill').fillna(False)

    avg_vol = df['volume'].shift(2).rolling(23).mean()
    breakout_long = (df['close'] > session_high) & (df['volume'] >= VOL_SPIKE_MULT * avg_vol)
    breakout_short = (df['close'] < session_low) & (df['volume'] >= VOL_SPIKE_MULT * avg_vol)
    broke_today_long = pd.Series(breakout_long.values, index=df.index).groupby(dates).cummax()
    broke_today_short = pd.Series(breakout_short.values, index=df.index).groupby(dates).cummax()

    retest_long = (df['close'] > session_high * (1 - RETEST_ZONE_PCT)) & (df['close'] < session_high * (1 + RETEST_ZONE_PCT))
    retest_short = (df['close'] > session_low * (1 - RETEST_ZONE_PCT)) & (df['close'] < session_low * (1 + RETEST_ZONE_PCT))
    held_above = (df['close'].shift(1) > session_high) & (df['close'].shift(2) > session_high)
    held_below = (df['close'].shift(1) < session_low) & (df['close'].shift(2) < session_low)

    entries_long = (broke_today_long & retest_long & held_above & trend_bullish).fillna(False)
    entries_short = (broke_today_short & retest_short & held_below & trend_bearish).fillna(False)

    sl_long = session_high * (1 - 0.005)
    tp_long = df['close'] + r_multiple * (df['close'] - sl_long)
    sl_short = session_low * (1 + 0.005)
    tp_short = df['close'] - r_multiple * (sl_short - df['close'])

    return simulate_trades(df, entries_long, entries_short, sl_long, tp_long, sl_short, tp_short, symbol)


if __name__ == "__main__":
    print("\n=== Variant A: S4 divergence-only (no demo fallback signal) ===")
    a_trades = []
    for sym in ["BTC/USDT:USDT", "ETH/USDT:USDT", "LINK/USDT:USDT", "DOGE/USDT:USDT"]:
        t = backtest_s4_divergence_only(sym)
        if t is not None:
            a_trades.append(t)
            summarize(t, f"  S4-divOnly {sym}")
    if a_trades:
        summarize(pd.concat(a_trades), "S4-divOnly TOTAL")

    print("\n=== Variant B: S2 wider profit target ===")
    for r in [1.5, 2.0, 2.5, 3.0]:
        b_trades = []
        for sym in ["SOL/USDT:USDT", "BNB/USDT:USDT"]:
            t = backtest_s2_wider_target(sym, r)
            if t is not None:
                b_trades.append(t)
        if b_trades:
            summarize(pd.concat(b_trades), f"S2 R={r}x TOTAL")
