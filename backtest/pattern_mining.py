"""Pattern-mining study: learn structural regularities from ~12 months of 1h
data, the honest way.

Method: split each symbol's history into DISCOVERY (first ~8 months) and
VALIDATION (last ~4 months, never touched during discovery). A pattern only
counts if its effect has the same sign and comparable size in BOTH windows —
that's the difference between market structure and memorized noise.

Patterns tested (pre-registered, deliberately few to limit data-dredging):
  P1  Daily momentum: does yesterday's direction predict today's return?
  P2  Hour-of-day drift: do specific UTC hours have systematic direction?
  P3  Volatility compression: after an unusually quiet day, is the next day's
      move bigger, and does its first-4h direction persist to day end?
  P4  Post-big-move (|z|>2, 4h): continuation or reversal over the next 24h?
  P5  Day-of-week effects on daily returns.

Uses the 365d 1h parquet cache created by s6_donchian_trend.py.
"""

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np
import pandas as pd

from backtest._common import fetch_ohlcv_cached

DAYS = 365
SYMBOLS = [
    "BTC/USDT:USDT", "ETH/USDT:USDT", "SOL/USDT:USDT", "BNB/USDT:USDT",
    "XRP/USDT:USDT", "LINK/USDT:USDT", "DOGE/USDT:USDT",
]
DISCOVERY_FRAC = 2 / 3


def load(symbol):
    df = fetch_ohlcv_cached(symbol, '1h', DAYS)
    if df.empty:
        return None, None
    cut = df.index[int(len(df) * DISCOVERY_FRAC)]
    return df[df.index < cut], df[df.index >= cut]


def stats_line(rets):
    """mean bps, hit rate, n, and a rough t-stat."""
    rets = pd.Series(rets).dropna()
    if len(rets) < 10:
        return None
    mean_bps = rets.mean() * 1e4
    tstat = rets.mean() / (rets.std() + 1e-12) * np.sqrt(len(rets))
    return {'mean_bps': mean_bps, 'hit': (rets > 0).mean() * 100, 'n': len(rets), 't': tstat}


def daily_frame(df):
    d = df.resample('1D').agg({'open': 'first', 'high': 'max', 'low': 'min', 'close': 'last', 'volume': 'sum'}).dropna()
    d['ret'] = d['close'].pct_change()
    d['range_pct'] = (d['high'] - d['low']) / d['close']
    return d


def p1_daily_momentum(df):
    d = daily_frame(df)
    after_up = d['ret'].shift(-1)[d['ret'] > 0]
    after_down = d['ret'].shift(-1)[d['ret'] < 0]
    return {'after_up_day': stats_line(after_up), 'after_down_day': stats_line(after_down)}


def p2_hour_of_day(df):
    r = df['close'].pct_change()
    out = {}
    for h in range(24):
        s = stats_line(r[r.index.hour == h])
        if s:
            out[h] = s
    return out


def p3_compression(df):
    d = daily_frame(df)
    quiet = d['range_pct'] < 0.6 * d['range_pct'].rolling(20).mean()
    next_abs = d['ret'].shift(-1).abs()
    expansion = {
        'next_day_absmove_after_quiet': stats_line(next_abs[quiet]),
        'next_day_absmove_baseline': stats_line(next_abs[~quiet]),
    }
    return expansion


def p4_post_big_move(df):
    r4 = df['close'].pct_change(4)
    z = (r4 - r4.rolling(24 * 30).mean()) / (r4.rolling(24 * 30).std() + 1e-12)
    fwd24 = df['close'].pct_change(24).shift(-24)
    return {
        'next24h_after_+2z': stats_line(fwd24[z > 2]),
        'next24h_after_-2z': stats_line(fwd24[z < -2]),
    }


def p5_day_of_week(df):
    d = daily_frame(df)
    out = {}
    for dow in range(7):
        s = stats_line(d['ret'][d.index.dayofweek == dow])
        if s:
            out[dow] = s
    return out


def fmt(s):
    if s is None:
        return "insufficient n"
    return f"{s['mean_bps']:+7.1f} bps  hit {s['hit']:4.1f}%  n={s['n']:5d}  t={s['t']:+.1f}"


if __name__ == "__main__":
    disc_frames, val_frames = {}, {}
    for sym in SYMBOLS:
        disc, val = load(sym)
        if disc is not None:
            disc_frames[sym], val_frames[sym] = disc, val

    def pooled(fn, frames):
        """Run per symbol and pool the raw conditional returns across symbols."""
        pooled_out = {}
        for sym, df in frames.items():
            res = fn(df)
            for key, s in res.items():
                pooled_out.setdefault(key, []).append((sym, s))
        return pooled_out

    for name, fn in [
        ("P1 daily momentum (next-day ret conditioned on today's sign)", p1_daily_momentum),
        ("P3 volatility compression (next-day |move| after quiet day)", p3_compression),
        ("P4 post-big-move 4h |z|>2 (next-24h ret)", p4_post_big_move),
    ]:
        print(f"\n===== {name} =====")
        for window, frames in [("DISCOVERY", disc_frames), ("VALIDATION", val_frames)]:
            print(f"  [{window}]")
            agg = pooled(fn, frames)
            for key, rows in agg.items():
                valid = [s for _, s in rows if s]
                if not valid:
                    continue
                mean_of_means = np.mean([s['mean_bps'] for s in valid])
                same_sign = sum(1 for s in valid if np.sign(s['mean_bps']) == np.sign(mean_of_means))
                total_n = sum(s['n'] for s in valid)
                print(f"    {key:32s} avg {mean_of_means:+7.1f} bps | same-sign {same_sign}/{len(valid)} symbols | n={total_n}")

    print("\n===== P2 hour-of-day drift (only hours with |avg| > 5 bps in discovery) =====")
    for window, frames in [("DISCOVERY", disc_frames), ("VALIDATION", val_frames)]:
        print(f"  [{window}]")
        by_hour = {}
        for sym, df in frames.items():
            for h, s in p2_hour_of_day(df).items():
                by_hour.setdefault(h, []).append(s['mean_bps'])
        for h in sorted(by_hour):
            avg = np.mean(by_hour[h])
            same = sum(1 for x in by_hour[h] if np.sign(x) == np.sign(avg))
            if window == "DISCOVERY" and abs(avg) <= 5:
                continue
            print(f"    hour {h:02d} UTC: avg {avg:+6.1f} bps/h | same-sign {same}/{len(by_hour[h])} symbols")

    print("\n===== P5 day-of-week (avg daily ret) =====")
    DOW = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun']
    for window, frames in [("DISCOVERY", disc_frames), ("VALIDATION", val_frames)]:
        print(f"  [{window}]")
        by_dow = {}
        for sym, df in frames.items():
            for dow, s in p5_day_of_week(df).items():
                by_dow.setdefault(dow, []).append(s['mean_bps'])
        for dow in sorted(by_dow):
            avg = np.mean(by_dow[dow])
            same = sum(1 for x in by_dow[dow] if np.sign(x) == np.sign(avg))
            print(f"    {DOW[dow]}: avg {avg:+7.1f} bps/day | same-sign {same}/{len(by_dow[dow])} symbols")
