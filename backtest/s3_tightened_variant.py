"""Test whether tightening S3's demo-relaxed filters back toward the
pre-relaxation intent rescues it, given the baseline (relaxed_current_params.py)
showed a catastrophic 13.6% win rate / -94% max drawdown / PF 0.34.

Changes vs. live: RSI 25/75 (was 35/65), ADX_MAX 30 (was 60 — an actual
"ranging market only" filter instead of one that almost never trips), and a
reversal-candle confirmation (current candle closes back toward the band
after touching it) as a lightweight stand-in for the engulfing check that was
stripped out for demo.
"""

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd
import pandas_ta as ta

from backtest._common import fetch_ohlcv_cached, simulate_trades, summarize
from strategies.strategy3_vwap_bands import VWAP_STDEV, RSI_PERIOD, ADX_PERIOD

DAYS = 180
RSI_OVERSOLD_TIGHT = 25
RSI_OVERBOUGHT_TIGHT = 75
ADX_MAX_TIGHT = 30


def backtest_s3_tight(symbol):
    df = fetch_ohlcv_cached(symbol, '5m', DAYS)
    df_1h = fetch_ohlcv_cached(symbol, '1h', DAYS)
    if df.empty or df_1h.empty or len(df) < 500:
        return None

    dates = df.index.date
    typical = (df['high'] + df['low'] + df['close']) / 3
    cum_vol = pd.Series(df['volume'].values, index=df.index).groupby(dates).cumsum()
    cum_vp = pd.Series((typical * df['volume']).values, index=df.index).groupby(dates).cumsum()
    vwap = cum_vp / cum_vol
    vwap_std = pd.Series(df['close'].values, index=df.index).groupby(dates).transform(lambda s: s.expanding(min_periods=1).std())
    upper_band = vwap + VWAP_STDEV * vwap_std
    lower_band = vwap - VWAP_STDEV * vwap_std

    rsi = pd.Series(df['close'].values, index=df.index).groupby(dates).transform(lambda s: ta.rsi(s, length=RSI_PERIOD))

    adx_1h = ta.adx(df_1h['high'], df_1h['low'], df_1h['close'], length=ADX_PERIOD)[f'ADX_{ADX_PERIOD}']
    adx_5m = adx_1h.reindex(df.index, method='ffill')
    regime_ok = (adx_5m <= ADX_MAX_TIGHT).fillna(False)

    # Reversal-candle confirmation: after touching the band, the CURRENT
    # candle must be closing back toward vwap (green after a lower-band
    # touch, red after an upper-band touch) — a cheap stand-in for engulfing.
    reversal_up = df['close'] > df['open']
    reversal_down = df['close'] < df['open']

    entries_long = (df['close'] <= lower_band) & (rsi < RSI_OVERSOLD_TIGHT) & regime_ok & reversal_up
    entries_short = (df['close'] >= upper_band) & (rsi > RSI_OVERBOUGHT_TIGHT) & regime_ok & reversal_down
    entries_long = entries_long.fillna(False)
    entries_short = entries_short.fillna(False)

    sl_long = lower_band * 0.9985
    tp_long = vwap
    sl_short = upper_band * 1.0015
    tp_short = vwap
    entries_long &= (df['close'] - sl_long) > 0
    entries_short &= (sl_short - df['close']) > 0

    return simulate_trades(df, entries_long, entries_short, sl_long, tp_long, sl_short, tp_short, symbol)


if __name__ == "__main__":
    print("=== S3 tightened (RSI 25/75, ADX_MAX 30, reversal-candle confirmation) ===")
    trades = []
    for sym in ["XRP/USDT:USDT", "ETH/USDT:USDT"]:
        t = backtest_s3_tight(sym)
        if t is not None:
            trades.append(t)
            summarize(t, f"  S3-tight {sym}")
    if trades:
        summarize(pd.concat(trades), "S3-tight TOTAL")
