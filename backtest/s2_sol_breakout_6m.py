import numpy as np
import pandas as pd
import ccxt
import time
from datetime import datetime, timezone, timedelta
import vectorbt as vbt

SYMBOL      = "SOL/USDT:USDT"
TIMEFRAME   = "5m"
SLIPPAGE    = 0.0012
COMMISSION  = 0.0002
EMA_FAST    = 9
EMA_MEDIUM  = 21
EMA_SLOW    = 50
VOL_SPIKE   = 1.5
ATR_PER     = 14

SL_MULTS = [1.0, 1.5, 2.0]
TP_MULTS = [2.0, 2.5, 3.0, 3.5]

def fetch_data(symbol, timeframe, since, until):
    exchange = ccxt.bybit({'options': {'defaultType': 'linear'}})
    all_candles = []
    while since < until:
        try:
            candles = exchange.fetch_ohlcv(symbol, timeframe, since=since, limit=1000)
        except Exception as e:
            print(f"Error: {e}, retrying...")
            time.sleep(10)
            continue
        if not candles:
            break
        all_candles += candles
        print(f"  Fetched {len(all_candles)} total... latest {pd.to_datetime(candles[-1][0], unit='ms')}")
        since = candles[-1][0] + 1
        time.sleep(exchange.rateLimit / 1000 * 2)
    if not all_candles:
        return pd.DataFrame()
    df = pd.DataFrame(all_candles, columns=['timestamp','open','high','low','close','volume'])
    df['datetime'] = pd.to_datetime(df['timestamp'], unit='ms', utc=True)
    df.set_index('datetime', inplace=True)
    return df

def add_daily_opening_range(df):
    """Add 'session_high' and 'session_low' columns based on first 3 bars of each UTC day."""
    # Group by date, take first 3 rows, get max high and min low
    daily_range = df.groupby(df.index.date).apply(
        lambda g: g.iloc[:3].agg({'high': 'max', 'low': 'min'})
    )
    # daily_range is a DataFrame with columns 'high' and 'low', indexed by date
    # Create a mapping Series for session_high and session_low
    session_high_map = daily_range['high']
    session_low_map = daily_range['low']
    # Map to each row in df using the date of the index
    df['session_high'] = df.index.to_series().apply(lambda dt: session_high_map.get(dt.date(), np.nan))
    df['session_low']  = df.index.to_series().apply(lambda dt: session_low_map.get(dt.date(), np.nan))
    return df

def strategy2_signals(df):
    # 1h trend filter
    df_1h = df.resample('1h').agg({
        'open': 'first', 'high': 'max', 'low': 'min', 'close': 'last', 'volume': 'sum'
    }).dropna()
    ema_fast_1h = vbt.MA.run(df_1h['close'], window=EMA_FAST, ewm=True).ma
    ema_medium_1h = vbt.MA.run(df_1h['close'], window=EMA_MEDIUM, ewm=True).ma
    ema_slow_1h = vbt.MA.run(df_1h['close'], window=EMA_SLOW, ewm=True).ma
    trend_bullish = (ema_fast_1h > ema_medium_1h) & (ema_medium_1h > ema_slow_1h)
    trend_bearish = (ema_fast_1h < ema_medium_1h) & (ema_medium_1h < ema_slow_1h)
    trend_bullish_5m = trend_bullish.reindex(df.index, method='ffill').fillna(False)
    trend_bearish_5m = trend_bearish.reindex(df.index, method='ffill').fillna(False)

    # Daily opening range (already in df columns)
    session_high = df['session_high']
    session_low  = df['session_low']

    avg_vol_20 = df['volume'].rolling(window=20).mean().shift(1)
    vol_ok = df['volume'] > VOL_SPIKE * avg_vol_20

    # Breakout candle
    break_long = (df['close'] > session_high) & vol_ok & trend_bullish_5m
    break_short = (df['close'] < session_low) & vol_ok & trend_bearish_5m

    # Retest: next candle must stay beyond breakout level
    retest_long = break_long.shift(1) & (df['close'] > session_high.shift(1))
    entries_long = retest_long.shift(1)
    retest_short = break_short.shift(1) & (df['close'] < session_low.shift(1))
    entries_short = retest_short.shift(1)

    entries_long = entries_long.fillna(False).infer_objects(copy=False)
    entries_short = entries_short.fillna(False).infer_objects(copy=False)

    # Time exit after 12 bars
    exit_long = pd.Series(False, index=df.index)
    exit_short = pd.Series(False, index=df.index)
    for idx in df.index[entries_long]:
        loc = df.index.get_loc(idx) + 12
        if loc < len(df.index):
            exit_long.iloc[loc] = True
    for idx in df.index[entries_short]:
        loc = df.index.get_loc(idx) + 12
        if loc < len(df.index):
            exit_short.iloc[loc] = True

    atr = vbt.ATR.run(df['high'], df['low'], df['close'], window=ATR_PER).atr
    return entries_long, entries_short, exit_long, exit_short, atr

def evaluate_params(df, sl_mult, tp_mult):
    entries_l, entries_s, exit_l, exit_s, atr = strategy2_signals(df)
    pf_long = vbt.Portfolio.from_signals(
        df['close'], entries=entries_l, exits=exit_l,
        sl_stop=df['close'] - sl_mult * atr,
        tp_stop=df['close'] + tp_mult * atr,
        direction='longonly', freq=TIMEFRAME, slippage=SLIPPAGE, fees=COMMISSION
    )
    pf_short = vbt.Portfolio.from_signals(
        df['close'], entries=entries_s, exits=exit_s,
        sl_stop=df['close'] + sl_mult * atr,
        tp_stop=df['close'] - tp_mult * atr,
        direction='shortonly', freq=TIMEFRAME, slippage=SLIPPAGE, fees=COMMISSION
    )
    all_trades = pd.concat([pf_long.trades.records_readable, pf_short.trades.records_readable])
    if len(all_trades) == 0:
        return -1.0, 0
    profit = all_trades[all_trades['PnL'] > 0]['PnL'].sum()
    loss = abs(all_trades[all_trades['PnL'] < 0]['PnL'].sum())
    pf = profit / loss if loss > 0 else float('inf')
    return pf, len(all_trades)

def walk_forward_optimize(df, window_size_days=60, step_days=30):
    total_profit = 0.0
    total_loss = 0.0
    total_trades = 0
    total_wins = 0
    max_dd = 0.0
    start = df.index[0]
    end = df.index[-1]
    window_start = start
    windows_info = []

    while window_start + pd.DateOffset(days=window_size_days) <= end:
        window_end = window_start + pd.DateOffset(days=window_size_days)
        train_end = window_start + pd.DateOffset(days=window_size_days - step_days)
        train_mask = (df.index >= window_start) & (df.index < train_end)
        test_mask  = (df.index >= train_end) & (df.index < window_end)
        train_df = df.loc[train_mask].copy()
        test_df  = df.loc[test_mask].copy()
        if len(train_df) < 200 or len(test_df) < 200:
            window_start += pd.DateOffset(days=step_days)
            continue

        best_pf = -1.0
        best_sl, best_tp = SL_MULTS[0], TP_MULTS[0]
        for sl in SL_MULTS:
            for tp in TP_MULTS:
                pf, _ = evaluate_params(train_df, sl, tp)
                if pf > best_pf:
                    best_pf = pf
                    best_sl, best_tp = sl, tp

        entries_l, entries_s, exit_l, exit_s, atr = strategy2_signals(test_df)
        pf_l = vbt.Portfolio.from_signals(
            test_df['close'], entries=entries_l, exits=exit_l,
            sl_stop=test_df['close'] - best_sl * atr,
            tp_stop=test_df['close'] + best_tp * atr,
            direction='longonly', freq=TIMEFRAME, slippage=SLIPPAGE, fees=COMMISSION
        )
        pf_s = vbt.Portfolio.from_signals(
            test_df['close'], entries=entries_s, exits=exit_s,
            sl_stop=test_df['close'] + best_sl * atr,
            tp_stop=test_df['close'] - best_tp * atr,
            direction='shortonly', freq=TIMEFRAME, slippage=SLIPPAGE, fees=COMMISSION
        )
        trades = pd.concat([pf_l.trades.records_readable, pf_s.trades.records_readable])
        if len(trades) == 0:
            window_start += pd.DateOffset(days=step_days)
            continue

        profit = trades[trades['PnL'] > 0]['PnL'].sum()
        loss = abs(trades[trades['PnL'] < 0]['PnL'].sum())
        total_profit += profit
        total_loss += loss
        total_trades += len(trades)
        total_wins += len(trades[trades['PnL'] > 0])

        def safe(pf, k, d=0.0):
            try: return pf.stats().get(k, d)
            except: return d
        dd = max(safe(pf_l, 'Max Drawdown [%]'), safe(pf_s, 'Max Drawdown [%]'))
        if dd > max_dd:
            max_dd = dd

        wr = (len(trades[trades['PnL'] > 0]) / len(trades)) * 100 if len(trades) > 0 else 0
        win_pf = profit / loss if loss > 0 else float('inf')
        windows_info.append({
            'window': f"{train_end.date()} - {window_end.date()}",
            'best_sl': best_sl,
            'best_tp': best_tp,
            'trades': len(trades),
            'win_rate': wr,
            'pf': win_pf,
            'max_dd': dd
        })
        print(f"Window test {train_end.date()} -> {window_end.date()}: SL={best_sl},TP={best_tp}, Trades={len(trades)}, WR={wr:.1f}%, PF={win_pf:.2f}")
        window_start += pd.DateOffset(days=step_days)

    overall_pf = total_profit / total_loss if total_loss > 0 else float('inf')
    overall_wr = (total_wins / total_trades * 100) if total_trades > 0 else 0
    all_pnl = pd.concat([pf_l.trades.records_readable['PnL'], pf_s.trades.records_readable['PnL']])
    sharpe = all_pnl.mean() / all_pnl.std() * np.sqrt(365*24*12) if all_pnl.std() > 0 else 0.0

    return pd.DataFrame(windows_info), overall_pf, sharpe, max_dd, overall_wr, total_trades

if __name__ == "__main__":
    print(f"Downloading last 6 months of 5m data for {SYMBOL}...")
    end_date = datetime.now(timezone.utc)
    start_date = end_date - timedelta(days=180)
    exchange = ccxt.bybit()
    since_ms = exchange.parse8601(start_date.isoformat())
    until_ms = exchange.parse8601(end_date.isoformat())
    df = fetch_data(SYMBOL, TIMEFRAME, since_ms, until_ms)
    if df.empty:
        print("No data. Exit.")
        exit(1)
    print(f"Loaded {len(df)} candles from {df.index[0]} to {df.index[-1]}.\n")

    # Add opening range columns
    print("Computing daily opening ranges...")
    df = add_daily_opening_range(df)
    print("Done. Running walk-forward optimization...\n")

    windows_df, overall_pf, overall_sharpe, max_dd, overall_wr, total_trades = walk_forward_optimize(df)

    print("\n========== OPTIMIZED WALK-FORWARD RESULTS ==========")
    print(windows_df.to_string(index=False))

    print("\n========== OVERALL GATE SUMMARY ==========")
    checks = [
        ("Profit Factor > 1.3", overall_pf > 1.3, f"{overall_pf:.2f}"),
        ("Sharpe Ratio > 1.0", overall_sharpe > 1.0, f"{overall_sharpe:.2f}"),
        ("Max Drawdown < 20%", max_dd < 20.0, f"{max_dd:.2f}%"),
        ("Win Rate 45-62%", 45 <= overall_wr <= 62, f"{overall_wr:.1f}%"),
        ("Minimum 180 Trades", total_trades >= 180, f"{total_trades}")
    ]
    for desc, passed, val in checks:
        print(f"{'✅' if passed else '❌'} {desc}: {val}")
    if all(p for _, p, _ in checks):
        print("\n🚀 Strategy 2 passes all gates!")
    else:
        print("\n⚠️ Strategy 2 improved – review trade frequency and risk parameters.")
