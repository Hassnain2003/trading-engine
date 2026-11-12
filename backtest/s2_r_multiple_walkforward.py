"""Out-of-sample validation for the S2 wider-target finding in
relaxed_variants.py (R=2.0/2.5/3.0 all showed PF>1 in-sample over the same
6 months used to pick them — classic overfitting risk). This picks the best
R-multiple on a training window and scores it on the following unseen window,
rolling forward, so the reported PF reflects genuine out-of-sample performance.
"""

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd
import pandas_ta as ta

from backtest._common import fetch_ohlcv_cached, simulate_trades
from strategies.strategy2_breakout_retest import (
    OPENING_MINUTES, VOL_SPIKE_MULT, RETEST_ZONE_PCT, EMA_FAST, EMA_MEDIUM, EMA_SLOW,
)

DAYS = 180
R_CANDIDATES = [1.5, 2.0, 2.5, 3.0]
TRAIN_DAYS = 90
TEST_DAYS = 30


def build_s2_frame(symbol):
    """Entries + sl columns are independent of R; tp is computed per-R later."""
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
    sl_short = session_low * (1 + 0.005)

    return df, entries_long, entries_short, sl_long, sl_short


def pf_for_r(df, entries_long, entries_short, sl_long, sl_short, r):
    tp_long = df['close'] + r * (df['close'] - sl_long)
    tp_short = df['close'] - r * (sl_short - df['close'])
    trades = simulate_trades(df, entries_long, entries_short, sl_long, tp_long, sl_short, tp_short)
    if trades.empty:
        return -1.0, trades
    wins = trades[trades['pnl_pct'] > 0]['pnl_pct'].sum()
    losses = abs(trades[trades['pnl_pct'] <= 0]['pnl_pct'].sum())
    pf = wins / losses if losses > 0 else float('inf')
    return pf, trades


def walk_forward_for_symbol(symbol):
    built = build_s2_frame(symbol)
    if built is None:
        return pd.DataFrame()
    df, entries_long, entries_short, sl_long, sl_short = built

    start = df.index[0]
    end = df.index[-1]
    window_start = start
    all_test_trades = []
    fold_log = []

    while window_start + pd.Timedelta(days=TRAIN_DAYS + TEST_DAYS) <= end:
        train_end = window_start + pd.Timedelta(days=TRAIN_DAYS)
        test_end = train_end + pd.Timedelta(days=TEST_DAYS)

        train_mask = (df.index >= window_start) & (df.index < train_end)
        test_mask = (df.index >= train_end) & (df.index < test_end)

        best_r, best_pf = R_CANDIDATES[0], -1.0
        for r in R_CANDIDATES:
            pf, _ = pf_for_r(
                df.loc[train_mask], entries_long.loc[train_mask], entries_short.loc[train_mask],
                sl_long.loc[train_mask], sl_short.loc[train_mask], r,
            )
            if pf > best_pf:
                best_pf, best_r = pf, r

        test_pf, test_trades = pf_for_r(
            df.loc[test_mask], entries_long.loc[test_mask], entries_short.loc[test_mask],
            sl_long.loc[test_mask], sl_short.loc[test_mask], best_r,
        )
        if not test_trades.empty:
            all_test_trades.append(test_trades)
        fold_log.append({
            'symbol': symbol, 'train_end': train_end.date(), 'test_end': test_end.date(),
            'chosen_r': best_r, 'train_pf': round(best_pf, 2),
            'test_trades': len(test_trades), 'test_pf': round(test_pf, 2) if test_pf != -1.0 else None,
        })
        window_start += pd.Timedelta(days=TEST_DAYS)

    print(pd.DataFrame(fold_log).to_string(index=False))
    return pd.concat(all_test_trades) if all_test_trades else pd.DataFrame()


if __name__ == "__main__":
    all_trades = []
    for sym in ["SOL/USDT:USDT", "BNB/USDT:USDT"]:
        print(f"\n--- {sym} walk-forward folds (train {TRAIN_DAYS}d / test {TEST_DAYS}d) ---")
        t = walk_forward_for_symbol(sym)
        if not t.empty:
            all_trades.append(t)

    print("\n========== OUT-OF-SAMPLE TOTAL (R chosen fresh each fold, never seen at test time) ==========")
    if all_trades:
        trades = pd.concat(all_trades)
        wins = trades[trades['pnl_pct'] > 0]
        losses = trades[trades['pnl_pct'] <= 0]
        pf = wins['pnl_pct'].sum() / abs(losses['pnl_pct'].sum()) if len(losses) else float('inf')
        wr = len(wins) / len(trades) * 100
        avg = trades['pnl_pct'].mean() * 100
        print(f"trades={len(trades)}  WR={wr:.1f}%  PF={pf:.2f}  avg P&L/trade={avg:.3f}%")
    else:
        print("No out-of-sample trades produced.")
