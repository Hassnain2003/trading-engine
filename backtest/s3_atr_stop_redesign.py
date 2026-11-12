"""S3 redesign experiment: replace the fixed 0.15%-beyond-band stop-loss with
an ATR-adaptive stop.

Motivation (see backtest/s3_tightened_variant.py): tightening entry filters
cut S3's trade count 13x and drawdown -94% -> -18% but left win rate (~16%)
and PF (~0.35) unchanged — proving the entry filters were never the problem.
The fixed 0.15% stop is far inside normal 5m noise, so trades get stopped out
before mean reversion can play out.

Grid tested here (in-sample, both symbols):
  - stop: entry -/+ ATR_MULT * ATR(14, 5m), ATR_MULT in {1.0, 1.5, 2.0}
  - take-profit: 'vwap' (original mean-reversion thesis) or '1.5R'
  - entry filters: 'relaxed' (live: RSI 35/65, ADX<=60) or
                   'tight' (RSI 25/75, ADX<=30, reversal candle)

The winning region is then validated out-of-sample by
backtest/s3_atr_walkforward.py before anything touches live code.
"""

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd
import pandas_ta as ta

from backtest._common import fetch_ohlcv_cached, simulate_trades, summarize
from strategies.strategy3_vwap_bands import (
    VWAP_STDEV, RSI_PERIOD, RSI_OVERSOLD, RSI_OVERBOUGHT, ADX_PERIOD, ADX_MAX,
)

DAYS = 180
ATR_PERIOD = 14

ATR_MULTS = [1.0, 1.5, 2.0]
TP_MODES = ['vwap', '1.5R']
FILTER_MODES = ['relaxed', 'tight']

RSI_OS_TIGHT, RSI_OB_TIGHT, ADX_MAX_TIGHT = 25, 75, 30


def build_s3_frame(symbol):
    """Everything entry/stop-related that doesn't depend on the grid params."""
    df = fetch_ohlcv_cached(symbol, '5m', DAYS)
    df_1h = fetch_ohlcv_cached(symbol, '1h', DAYS)
    if df.empty or df_1h.empty or len(df) < 500:
        return None

    dates = df.index.date
    typical = (df['high'] + df['low'] + df['close']) / 3
    cum_vol = pd.Series(df['volume'].values, index=df.index).groupby(dates).cumsum()
    cum_vp = pd.Series((typical * df['volume']).values, index=df.index).groupby(dates).cumsum()
    vwap = cum_vp / cum_vol
    vwap_std = pd.Series(df['close'].values, index=df.index).groupby(dates).transform(
        lambda s: s.expanding(min_periods=1).std()
    )
    upper_band = vwap + VWAP_STDEV * vwap_std
    lower_band = vwap - VWAP_STDEV * vwap_std

    rsi = pd.Series(df['close'].values, index=df.index).groupby(dates).transform(
        lambda s: ta.rsi(s, length=RSI_PERIOD)
    )
    adx_1h = ta.adx(df_1h['high'], df_1h['low'], df_1h['close'], length=ADX_PERIOD)[f'ADX_{ADX_PERIOD}']
    adx_5m = adx_1h.reindex(df.index, method='ffill')

    atr = ta.atr(df['high'], df['low'], df['close'], length=ATR_PERIOD)

    return {
        'df': df, 'vwap': vwap, 'upper_band': upper_band, 'lower_band': lower_band,
        'rsi': rsi, 'adx': adx_5m, 'atr': atr,
    }


def run_config(frame, symbol, atr_mult, tp_mode, filter_mode):
    df = frame['df']
    vwap, upper_band, lower_band = frame['vwap'], frame['upper_band'], frame['lower_band']
    rsi, adx, atr = frame['rsi'], frame['adx'], frame['atr']

    if filter_mode == 'relaxed':
        regime_ok = (adx <= ADX_MAX).fillna(False)
        long_rsi = rsi < RSI_OVERSOLD
        short_rsi = rsi > RSI_OVERBOUGHT
        long_confirm = pd.Series(True, index=df.index)
        short_confirm = pd.Series(True, index=df.index)
    else:
        regime_ok = (adx <= ADX_MAX_TIGHT).fillna(False)
        long_rsi = rsi < RSI_OS_TIGHT
        short_rsi = rsi > RSI_OB_TIGHT
        long_confirm = df['close'] > df['open']
        short_confirm = df['close'] < df['open']

    entries_long = ((df['close'] <= lower_band) & long_rsi & regime_ok & long_confirm).fillna(False)
    entries_short = ((df['close'] >= upper_band) & short_rsi & regime_ok & short_confirm).fillna(False)

    sl_long = df['close'] - atr_mult * atr
    sl_short = df['close'] + atr_mult * atr

    if tp_mode == 'vwap':
        tp_long = vwap
        tp_short = vwap
        # Entry must still be below (above) the target for the trade to make sense
        entries_long &= (vwap - df['close']) > 0
        entries_short &= (df['close'] - vwap) > 0
    else:
        tp_long = df['close'] + 1.5 * (df['close'] - sl_long)
        tp_short = df['close'] - 1.5 * (sl_short - df['close'])

    entries_long &= atr.notna()
    entries_short &= atr.notna()

    return simulate_trades(df, entries_long, entries_short, sl_long, tp_long, sl_short, tp_short, symbol)


if __name__ == "__main__":
    frames = {}
    for sym in ["XRP/USDT:USDT", "ETH/USDT:USDT"]:
        frames[sym] = build_s3_frame(sym)

    results = []
    for filter_mode in FILTER_MODES:
        for tp_mode in TP_MODES:
            for atr_mult in ATR_MULTS:
                combined = []
                for sym, frame in frames.items():
                    if frame is None:
                        continue
                    t = run_config(frame, sym, atr_mult, tp_mode, filter_mode)
                    if t is not None and not t.empty:
                        combined.append(t)
                label = f"S3 {filter_mode:7s} stop={atr_mult}xATR tp={tp_mode:5s}"
                if combined:
                    r = summarize(pd.concat(combined), label)
                    r.update({'filter': filter_mode, 'tp': tp_mode, 'atr_mult': atr_mult})
                    results.append(r)
                else:
                    print(f"{label}: no trades")

    print("\n========== RANKED BY PF (both symbols combined, ~6 months, in-sample) ==========")
    ranked = sorted([r for r in results if r['trades'] > 0], key=lambda r: r['pf'], reverse=True)
    for r in ranked:
        print(
            f"filter={r['filter']:7s} stop={r['atr_mult']}xATR tp={r['tp']:5s}  "
            f"trades={r['trades']:5d}  WR={r['win_rate']:5.1f}%  PF={r['pf']:.2f}  "
            f"MaxDD={r['max_dd_pct']:6.1f}%  avg={r['avg_pnl_pct']:.3f}%"
        )
