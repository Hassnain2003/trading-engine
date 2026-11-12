import numpy as np
import pandas as pd
import ccxt
import time
from datetime import datetime, timezone, timedelta
import vectorbt as vbt

SYMBOL      = "DOGE/USDT:USDT"
TIMEFRAME   = "5m"
SLIPPAGE    = 0.001
COMMISSION  = 0.0002

# Grid search space
FAST_RANGE   = [5, 9, 13, 17]
SLOW_RANGE   = [21, 30, 40, 50]
TREND_RANGE  = [30, 50, 70]
SL_MULTS     = [1.5, 2.0, 2.5]
TP_MULTS     = [2.0, 2.5, 3.0, 3.5]
VOL_WINDOWS  = [3, 5, 10]
ATR_PERIOD   = 14

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

def strategy5_signals(df, fast, slow, trend, vol_window):
    # 1h trend filter
    df_1h = df.resample('1h').agg({
        'open': 'first', 'high': 'max', 'low': 'min', 'close': 'last', 'volume': 'sum'
    }).dropna()
    ema_trend = vbt.MA.run(df_1h['close'], window=trend, ewm=True).ma
    trend_bullish = df_1h['close'] > ema_trend
    trend_5m = trend_bullish.reindex(df.index, method='ffill').fillna(False)

    ema_fast = vbt.MA.run(df['close'], window=fast, ewm=True).ma
    ema_slow = vbt.MA.run(df['close'], window=slow, ewm=True).ma

    cross_up = (ema_fast > ema_slow) & (ema_fast.shift(1) <= ema_slow.shift(1))
    cross_down = (ema_fast < ema_slow) & (ema_fast.shift(1) >= ema_slow.shift(1))

    avg_vol = df['volume'].rolling(window=vol_window).mean().shift(1)
    vol_spike = df['volume'] > avg_vol

    entries_long = trend_5m & cross_up & vol_spike
    entries_short = (~trend_5m) & cross_down & vol_spike

    # Time exit after 24 bars (2 hours)
    exit_long = pd.Series(False, index=df.index)
    exit_short = pd.Series(False, index=df.index)
    entry_idx_l = df.index[entries_long]
    entry_idx_s = df.index[entries_short]
    for idx in entry_idx_l:
        loc = df.index.get_loc(idx) + 24
        if loc < len(df.index):
            exit_long.iloc[loc] = True
    for idx in entry_idx_s:
        loc = df.index.get_loc(idx) + 24
        if loc < len(df.index):
            exit_short.iloc[loc] = True

    atr = vbt.ATR.run(df['high'], df['low'], df['close'], window=ATR_PERIOD).atr
    return entries_long, entries_short, exit_long, exit_short, atr

def evaluate_params(df, fast, slow, trend, vol_window, sl_mult, tp_mult):
    entries_l, entries_s, exit_l, exit_s, atr = strategy5_signals(df, fast, slow, trend, vol_window)
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
        return -1.0, 0, None
    profit = all_trades[all_trades['PnL'] > 0]['PnL'].sum()
    loss = abs(all_trades[all_trades['PnL'] < 0]['PnL'].sum())
    pf = profit / loss if loss > 0 else float('inf')
    return pf, len(all_trades), all_trades

def walk_forward_optimize(df, window_size_days=90, step_days=30):
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
        train_end = window_start + pd.DateOffset(days=60)
        train_mask = (df.index >= window_start) & (df.index < train_end)
        test_mask = (df.index >= train_end) & (df.index < window_end)
        train_df = df.loc[train_mask].copy()
        test_df = df.loc[test_mask].copy()
        if len(train_df) < 200 or len(test_df) < 200:
            window_start += pd.DateOffset(days=step_days)
            continue

        # Grid search on train data
        best_pf = -1.0
        best_params = (FAST_RANGE[0], SLOW_RANGE[0], TREND_RANGE[0], VOL_WINDOWS[0], SL_MULTS[0], TP_MULTS[0])
        for fast in FAST_RANGE:
            for slow in SLOW_RANGE:
                if fast >= slow:
                    continue
                for trend in TREND_RANGE:
                    for vol_win in VOL_WINDOWS:
                        for sl in SL_MULTS:
                            for tp in TP_MULTS:
                                pf, _, _ = evaluate_params(train_df, fast, slow, trend, vol_win, sl, tp)
                                if pf > best_pf:
                                    best_pf = pf
                                    best_params = (fast, slow, trend, vol_win, sl, tp)

        # Test best params on out-of-sample
        fast_b, slow_b, trend_b, vol_b, sl_b, tp_b = best_params
        entries_l, entries_s, exit_l, exit_s, atr = strategy5_signals(test_df, fast_b, slow_b, trend_b, vol_b)
        pf_l = vbt.Portfolio.from_signals(
            test_df['close'], entries=entries_l, exits=exit_l,
            sl_stop=test_df['close'] - sl_b * atr,
            tp_stop=test_df['close'] + tp_b * atr,
            direction='longonly', freq=TIMEFRAME, slippage=SLIPPAGE, fees=COMMISSION
        )
        pf_s = vbt.Portfolio.from_signals(
            test_df['close'], entries=entries_s, exits=exit_s,
            sl_stop=test_df['close'] + sl_b * atr,
            tp_stop=test_df['close'] - tp_b * atr,
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
            'best_params': f"F={fast_b},S={slow_b},T={trend_b},VW={vol_b},SL={sl_b},TP={tp_b}",
            'trades': len(trades),
            'win_rate': wr,
            'pf': win_pf,
            'max_dd': dd
        })
        print(f"Window test {train_end.date()} -> {window_end.date()}: Params={best_params}, Trades={len(trades)}, WR={wr:.1f}%, PF={win_pf:.2f}")
        window_start += pd.DateOffset(days=step_days)

    overall_pf = total_profit / total_loss if total_loss > 0 else float('inf')
    overall_wr = (total_wins / total_trades * 100) if total_trades > 0 else 0
    all_pnl = pd.concat([pf_l.trades.records_readable['PnL'], pf_s.trades.records_readable['PnL']])
    sharpe = all_pnl.mean() / all_pnl.std() * np.sqrt(365*24*12) if all_pnl.std() > 0 else 0.0

    return pd.DataFrame(windows_info), overall_pf, sharpe, max_dd, overall_wr, total_trades

if __name__ == "__main__":
    print(f"Downloading 6 months of {TIMEFRAME} data for {SYMBOL}...")
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
        print("\n🚀 Strategy 5 passes all gates!")
    else:
        print("\n⚠️ Strategy 5 needs further adjustment or coin reassignment.")
