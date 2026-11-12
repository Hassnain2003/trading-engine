"""Re-backtest of S2/S3/S4 using the EXACT parameters currently live in
strategies/strategy2_breakout_retest.py, strategy3_vwap_bands.py, and
strategy4_rsi_macd.py (the "DEMO relaxed" thresholds), not the older,
differently-tuned versions in backtest/s2_*.py, s3_*.py, s4_*.py.

Constants are imported directly from the live strategy modules so this can
never silently drift out of sync with what's actually trading.

Known simplifications (documented rather than hidden):
  - S3's live trailing-stop (position_manager.py) is NOT simulated — this
    backtest uses the fixed initial stop/target only, which is a conservative
    (pessimistic) simplification since the real trailing stop only ever locks
    in MORE profit, never less.
  - S4's live code recomputes RSI/MACD on a rolling last-60-bar window each
    call; this backtest computes them once continuously over full history
    (standard practice) — values may differ slightly right after a regime
    change, but the entry/exit RULES are identical.
  - Execution cost is modeled as a flat slippage+fee (see backtest/_common.py),
    not per-trade real order-book depth.
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
from strategies.strategy3_vwap_bands import (
    VWAP_STDEV, RSI_PERIOD as S3_RSI_PERIOD, RSI_OVERSOLD, RSI_OVERBOUGHT, ADX_PERIOD, ADX_MAX,
)
from strategies.strategy4_rsi_macd import (
    RSI_PERIOD as S4_RSI_PERIOD, MACD_FAST, MACD_SLOW, MACD_SIGNAL, RSI_OS, RSI_OB,
)

DAYS = 180  # ~6 months, matches the existing backtest/ convention


# ─────────────────────────── Strategy 2: Breakout Retest ───────────────────────────

def backtest_s2(symbol):
    df = fetch_ohlcv_cached(symbol, '5m', DAYS)
    if df.empty or len(df) < 500:
        print(f"  {symbol}: insufficient data")
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

    # Live: avg_vol = last-25-bars volume mean, excluding the most recent 2 bars.
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
    tp_long = df['close'] + 1.5 * (df['close'] - sl_long)
    sl_short = session_low * (1 + 0.005)
    tp_short = df['close'] - 1.5 * (sl_short - df['close'])

    trades = simulate_trades(df, entries_long, entries_short, sl_long, tp_long, sl_short, tp_short, symbol)
    return trades


# ─────────────────────────────── Strategy 3: VWAP Bands ───────────────────────────────

def backtest_s3(symbol):
    df = fetch_ohlcv_cached(symbol, '5m', DAYS)
    df_1h = fetch_ohlcv_cached(symbol, '1h', DAYS)
    if df.empty or df_1h.empty or len(df) < 500:
        print(f"  {symbol}: insufficient data")
        return None

    dates = df.index.date
    typical = (df['high'] + df['low'] + df['close']) / 3
    cum_vol = pd.Series((df['volume'].values), index=df.index).groupby(dates).cumsum()
    cum_vp = pd.Series((typical * df['volume']).values, index=df.index).groupby(dates).cumsum()
    vwap = cum_vp / cum_vol
    vwap_std = pd.Series(df['close'].values, index=df.index).groupby(dates).transform(lambda s: s.expanding(min_periods=1).std())
    upper_band = vwap + VWAP_STDEV * vwap_std
    lower_band = vwap - VWAP_STDEV * vwap_std

    rsi = pd.Series(df['close'].values, index=df.index).groupby(dates).transform(lambda s: ta.rsi(s, length=S3_RSI_PERIOD))

    adx_1h = ta.adx(df_1h['high'], df_1h['low'], df_1h['close'], length=ADX_PERIOD)[f'ADX_{ADX_PERIOD}']
    adx_5m = adx_1h.reindex(df.index, method='ffill')
    regime_ok = (adx_5m <= ADX_MAX).fillna(False)

    entries_long = (df['close'] <= lower_band) & (rsi < RSI_OVERSOLD) & regime_ok
    entries_short = (df['close'] >= upper_band) & (rsi > RSI_OVERBOUGHT) & regime_ok
    entries_long = entries_long.fillna(False)
    entries_short = entries_short.fillna(False)

    sl_long = lower_band * 0.9985
    tp_long = vwap
    sl_short = upper_band * 1.0015
    tp_short = vwap
    valid_long = (df['close'] - sl_long) > 0
    valid_short = (sl_short - df['close']) > 0
    entries_long &= valid_long
    entries_short &= valid_short

    trades = simulate_trades(df, entries_long, entries_short, sl_long, tp_long, sl_short, tp_short, symbol)
    return trades


# ───────────────────────── Strategy 4: RSI Divergence + MACD ─────────────────────────

def backtest_s4(symbol):
    df = fetch_ohlcv_cached(symbol, '15m', DAYS)
    if df.empty or len(df) < 500:
        print(f"  {symbol}: insufficient data")
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

    recent_lows, recent_highs = [], []  # indices, trimmed to a 25-bar trailing window
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

        fired = False
        if len(recent_lows) >= 2:
            i1, i2 = recent_lows[-2], recent_lows[-1]
            if close.iloc[i2] < close.iloc[i1] and rsi.iloc[i2] > rsi.iloc[i1]:
                macd_confirms = (macd_hist.iloc[i] > macd_hist.iloc[i - 1]) or (macd_line.iloc[i] > macd_signal.iloc[i])
                if macd_confirms:
                    entries_long[i] = True
                    fired = True
        if len(recent_highs) >= 2:
            i1, i2 = recent_highs[-2], recent_highs[-1]
            if close.iloc[i2] > close.iloc[i1] and rsi.iloc[i2] < rsi.iloc[i1]:
                macd_confirms = (macd_hist.iloc[i] < macd_hist.iloc[i - 1]) or (macd_line.iloc[i] < macd_signal.iloc[i])
                if macd_confirms:
                    entries_short[i] = True
                    fired = True

        # RSI-extreme + MACD-cross fallback (only if divergence didn't already fire)
        if not fired and i > 0:
            bull_cross = macd_line.iloc[i] > macd_signal.iloc[i] and macd_line.iloc[i - 1] <= macd_signal.iloc[i - 1]
            bear_cross = macd_line.iloc[i] < macd_signal.iloc[i] and macd_line.iloc[i - 1] >= macd_signal.iloc[i - 1]
            if rsi.iloc[i] < RSI_OS and bull_cross:
                entries_long[i] = True
            elif rsi.iloc[i] > RSI_OB and bear_cross:
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

    trades = simulate_trades(df, entries_long, entries_short, sl_long, tp_long, sl_short, tp_short, symbol)
    return trades


if __name__ == "__main__":
    results = []

    print("\n=== Strategy 2: Breakout Retest (SOL, BNB) — current relaxed params ===")
    s2_trades = []
    for sym in ["SOL/USDT:USDT", "BNB/USDT:USDT"]:
        print(f"  downloading/backtesting {sym}...")
        t = backtest_s2(sym)
        if t is not None:
            s2_trades.append(t)
            results.append(summarize(t, f"  S2 {sym}"))
    if s2_trades:
        results.append(summarize(pd.concat(s2_trades), "S2 TOTAL"))

    print("\n=== Strategy 3: VWAP Bands (XRP, ETH) — current relaxed params ===")
    s3_trades = []
    for sym in ["XRP/USDT:USDT", "ETH/USDT:USDT"]:
        print(f"  downloading/backtesting {sym}...")
        t = backtest_s3(sym)
        if t is not None:
            s3_trades.append(t)
            results.append(summarize(t, f"  S3 {sym}"))
    if s3_trades:
        results.append(summarize(pd.concat(s3_trades), "S3 TOTAL"))

    print("\n=== Strategy 4: RSI Divergence + MACD (BTC, ETH, LINK, DOGE) — current relaxed params ===")
    s4_trades = []
    for sym in ["BTC/USDT:USDT", "ETH/USDT:USDT", "LINK/USDT:USDT", "DOGE/USDT:USDT"]:
        print(f"  downloading/backtesting {sym}...")
        t = backtest_s4(sym)
        if t is not None:
            s4_trades.append(t)
            results.append(summarize(t, f"  S4 {sym}"))
    if s4_trades:
        results.append(summarize(pd.concat(s4_trades), "S4 TOTAL"))

    print("\n\n========== SUMMARY (current live/relaxed parameters, ~6 months) ==========")
    for r in results:
        if r['trades'] == 0:
            print(f"{r['label']:20s}  0 trades")
        else:
            print(
                f"{r['label']:20s}  trades={r['trades']:4d}  WR={r['win_rate']:5.1f}%  "
                f"PF={r['pf']:.2f}  MaxDD={r['max_dd_pct']:6.1f}%  avgP&L/trade={r['avg_pnl_pct']:.3f}%"
            )
