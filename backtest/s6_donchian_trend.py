"""S6 candidate: Donchian-breakout trend following on 1h with a 4h trend
filter and ATR-based stops.

Rationale: S3/S4 died on 5m noise (see backtest/relaxed_current_params.py
and s3_atr_stop_redesign.py). Trend breakouts on higher timeframes are the
most durable documented edge class; costs also shrink relative to trade size.

Structure (deliberately expressible as entry + fixed SL/TP, since the live
engine only supports SL/TP exits attached after fill):
  - Entry long:  1h close breaks above the prior N-bar highest high, AND the
                 4h trend agrees (EMA50 > EMA200 on 4h, resampled from 1h).
  - Entry short: mirror image.
  - Stop: entry -/+ ATR_MULT * ATR(14, 1h).
  - Target: R_MULT * stop distance (wide, to let trends run).

Grid: N in {20, 55} (turtle-style fast/slow), ATR_MULT in {1.5, 2.0, 2.5},
R_MULT in {2.0, 3.0, 4.0}. 365 days, 7 symbols. Winner (if any) goes to
walk-forward validation in backtest/s6_walkforward.py before touching live.
"""

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd
import pandas_ta as ta

from backtest._common import fetch_ohlcv_cached, simulate_trades, summarize

DAYS = 365
SYMBOLS = [
    "BTC/USDT:USDT", "ETH/USDT:USDT", "SOL/USDT:USDT", "BNB/USDT:USDT",
    "XRP/USDT:USDT", "LINK/USDT:USDT", "DOGE/USDT:USDT",
]

DONCHIAN_NS = [20, 55]
ATR_MULTS = [1.5, 2.0, 2.5]
R_MULTS = [2.0, 3.0, 4.0]

ATR_PERIOD = 14
TREND_EMA_FAST = 50
TREND_EMA_SLOW = 200


def build_frame(symbol):
    df = fetch_ohlcv_cached(symbol, '1h', DAYS)
    if df.empty or len(df) < 1500:
        return None

    df_4h = df.resample('4h').agg(
        {'open': 'first', 'high': 'max', 'low': 'min', 'close': 'last', 'volume': 'sum'}
    ).dropna()
    ema_fast_4h = ta.ema(df_4h['close'], length=TREND_EMA_FAST)
    ema_slow_4h = ta.ema(df_4h['close'], length=TREND_EMA_SLOW)
    uptrend = (ema_fast_4h > ema_slow_4h).reindex(df.index, method='ffill').fillna(False)
    downtrend = (ema_fast_4h < ema_slow_4h).reindex(df.index, method='ffill').fillna(False)

    atr = ta.atr(df['high'], df['low'], df['close'], length=ATR_PERIOD)
    return {'df': df, 'uptrend': uptrend, 'downtrend': downtrend, 'atr': atr}


def run_config(frame, symbol, donchian_n, atr_mult, r_mult):
    df, atr = frame['df'], frame['atr']
    upper = df['high'].rolling(donchian_n).max().shift(1)
    lower = df['low'].rolling(donchian_n).min().shift(1)

    entries_long = ((df['close'] > upper) & frame['uptrend'] & atr.notna()).fillna(False)
    entries_short = ((df['close'] < lower) & frame['downtrend'] & atr.notna()).fillna(False)

    sl_long = df['close'] - atr_mult * atr
    tp_long = df['close'] + r_mult * atr_mult * atr
    sl_short = df['close'] + atr_mult * atr
    tp_short = df['close'] - r_mult * atr_mult * atr

    return simulate_trades(df, entries_long, entries_short, sl_long, tp_long, sl_short, tp_short, symbol)


if __name__ == "__main__":
    frames = {}
    for sym in SYMBOLS:
        print(f"  loading {sym}...")
        frames[sym] = build_frame(sym)

    results = []
    for n in DONCHIAN_NS:
        for atr_mult in ATR_MULTS:
            for r_mult in R_MULTS:
                combined = []
                for sym, frame in frames.items():
                    if frame is None:
                        continue
                    t = run_config(frame, sym, n, atr_mult, r_mult)
                    if t is not None and not t.empty:
                        combined.append(t)
                label = f"S6 N={n:2d} stop={atr_mult}xATR tp={r_mult}R"
                if combined:
                    r = summarize(pd.concat(combined), label)
                    r.update({'n': n, 'atr_mult': atr_mult, 'r_mult': r_mult})
                    results.append(r)

    print("\n========== RANKED BY PF (7 symbols, 365 days, in-sample) ==========")
    ranked = sorted([r for r in results if r['trades'] > 0], key=lambda r: r['pf'], reverse=True)
    for r in ranked:
        print(
            f"N={r['n']:2d} stop={r['atr_mult']}xATR tp={r['r_mult']}R  "
            f"trades={r['trades']:5d}  WR={r['win_rate']:5.1f}%  PF={r['pf']:.2f}  "
            f"MaxDD={r['max_dd_pct']:6.1f}%  avg={r['avg_pnl_pct']:.3f}%"
        )
