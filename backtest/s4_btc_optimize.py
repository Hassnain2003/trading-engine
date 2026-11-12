import numpy as np
import pandas as pd
import pandas_ta as ta
import ccxt
import time
from datetime import datetime, timezone, timedelta
import vectorbt as vbt
from scipy.signal import argrelextrema

SYMBOL      = "BTC/USDT:USDT"
TIMEFRAME   = "15m"
SLIPPAGE    = 0.0005
COMMISSION  = 0.0002
MACD_FAST   = 12
MACD_SLOW   = 26
MACD_SIGNAL = 9
VOL_PERIOD  = 20

# Parameter grids
SL_MULTS   = [1.0, 1.5, 2.0]
TP_MULTS   = [2.0, 2.5, 3.0, 3.5]
RSI_VALS   = [10, 14, 20]
SWING_VALS = [2, 3]

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

def strategy4_signals(df, rsi_per, swing_order):
    rsi = vbt.RSI.run(df['close'], window=rsi_per).rsi
    macd_df = ta.macd(df['close'], fast=MACD_FAST, slow=MACD_SLOW, signal=MACD_SIGNAL)
    macd_line = macd_df[f'MACD_{MACD_FAST}_{MACD_SLOW}_{MACD_SIGNAL}']
    macd_signal = macd_df[f'MACDs_{MACD_FAST}_{MACD_SLOW}_{MACD_SIGNAL}']
    macd_hist = macd_df[f'MACDh_{MACD_FAST}_{MACD_SLOW}_{MACD_SIGNAL}']

    avg_vol = df['volume'].rolling(window=VOL_PERIOD).mean().shift(1)

    swing_low_idx = argrelextrema(df['low'].values, np.less, order=swing_order)[0]
    swing_high_idx = argrelextrema(df['high'].values, np.greater, order=swing_order)[0]

    entries_long = pd.Series(False, index=df.index)
    entries_short = pd.Series(False, index=df.index)

    for i in range(len(df)):
        if i < 20:
            continue
        if len(swing_low_idx) >= 2:
            relevant_lows = swing_low_idx[swing_low_idx <= i]
            if len(relevant_lows) >= 2:
                idx1, idx2 = relevant_lows[-2], relevant_lows[-1]
                price1, price2 = df['low'].iloc[idx1], df['low'].iloc[idx2]
                rsi1, rsi2 = rsi.iloc[idx1], rsi.iloc[idx2]
                if price2 < price1 and rsi2 > rsi1:
                    if (macd_hist.iloc[i] > macd_hist.iloc[i-1] and macd_hist.iloc[i-1] <= 0) or \
                       (macd_line.iloc[i] > macd_signal.iloc[i] and macd_line.iloc[i-1] <= macd_signal.iloc[i-1]):
                        if df['volume'].iloc[i] > avg_vol.iloc[i]:
                            entries_long.iloc[i] = True
        if len(swing_high_idx) >= 2:
            relevant_highs = swing_high_idx[swing_high_idx <= i]
            if len(relevant_highs) >= 2:
                idx1, idx2 = relevant_highs[-2], relevant_highs[-1]
                price1, price2 = df['high'].iloc[idx1], df['high'].iloc[idx2]
                rsi1, rsi2 = rsi.iloc[idx1], rsi.iloc[idx2]
                if price2 > price1 and rsi2 < rsi1:
                    if (macd_hist.iloc[i] < macd_hist.iloc[i-1] and macd_hist.iloc[i-1] >= 0) or \
                       (macd_line.iloc[i] < macd_signal.iloc[i] and macd_line.iloc[i-1] >= macd_signal.iloc[i-1]):
                        if df['volume'].iloc[i] > avg_vol.iloc[i]:
                            entries_short.iloc[i] = True

    # MACD cross exit
    exit_long = (macd_line.shift(1) >= macd_signal.shift(1)) & (macd_line < macd_signal)
    exit_short = (macd_line.shift(1) <= macd_signal.shift(1)) & (macd_line > macd_signal)
    exit_long = exit_long.fillna(False)
    exit_short = exit_short.fillna(False)

    atr = vbt.ATR.run(df['high'], df['low'], df['close'], window=14).atr  # ATR period fixed at 14
    return entries_long, entries_short, exit_long, exit_short, atr

def evaluate_params(df, rsi_per, swing_order, sl_mult, tp_mult):
    entries_l, entries_s, exit_l, exit_s, atr = strategy4_signals(df, rsi_per, swing_order)
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
        return -1.0, None
    gross_profit = all_trades[all_trades['PnL'] > 0]['PnL'].sum()
    gross_loss = abs(all_trades[all_trades['PnL'] < 0]['PnL'].sum())
    if gross_loss == 0:
        pf = float('inf')
    else:
        pf = gross_profit / gross_loss
    return pf, all_trades

def walk_forward_optimize(df, window_size_days=60, step_days=30):
    total_gross_profit = 0.0
    total_gross_loss = 0.0
    total_trades_all = 0
    total_wins = 0
    max_dd_overall = 0.0
    start_date = df.index[0]
    end_date = df.index[-1]
    window_start = start_date
    windows_info = []

    while window_start + pd.DateOffset(days=window_size_days) <= end_date:
        window_end = window_start + pd.DateOffset(days=window_size_days)
        train_end = window_start + pd.DateOffset(days=window_size_days - step_days)  # in-sample up to step_days before end
        train_mask = (df.index >= window_start) & (df.index < train_end)
        test_mask  = (df.index >= train_end) & (df.index < window_end)
        train_df = df.loc[train_mask].copy()
        test_df  = df.loc[test_mask].copy()
        if len(train_df) < 200 or len(test_df) < 200:
            window_start += pd.DateOffset(days=step_days)
            continue

        # Grid search on train data
        best_pf = -1.0
        best_params = None
        best_trades = None
        for sl in SL_MULTS:
            for tp in TP_MULTS:
                for rsi_per in RSI_VALS:
                    for sw in SWING_VALS:
                        pf, trades = evaluate_params(train_df, rsi_per, sw, sl, tp)
                        if pf > best_pf:
                            best_pf = pf
                            best_params = (rsi_per, sw, sl, tp)
                            best_trades = trades

        # Test best params on test data
        rsi_best, sw_best, sl_best, tp_best = best_params
        entries_l, entries_s, exit_l, exit_s, atr = strategy4_signals(test_df, rsi_best, sw_best)
        pf_long = vbt.Portfolio.from_signals(
            test_df['close'], entries=entries_l, exits=exit_l,
            sl_stop=test_df['close'] - sl_best * atr,
            tp_stop=test_df['close'] + tp_best * atr,
            direction='longonly', freq=TIMEFRAME, slippage=SLIPPAGE, fees=COMMISSION
        )
        pf_short = vbt.Portfolio.from_signals(
            test_df['close'], entries=entries_s, exits=exit_s,
            sl_stop=test_df['close'] + sl_best * atr,
            tp_stop=test_df['close'] - tp_best * atr,
            direction='shortonly', freq=TIMEFRAME, slippage=SLIPPAGE, fees=COMMISSION
        )
        all_trades = pd.concat([pf_long.trades.records_readable, pf_short.trades.records_readable])
        if len(all_trades) == 0:
            window_start += pd.DateOffset(days=step_days)
            continue

        gross_profit = all_trades[all_trades['PnL'] > 0]['PnL'].sum()
        gross_loss = abs(all_trades[all_trades['PnL'] < 0]['PnL'].sum())
        total_gross_profit += gross_profit
        total_gross_loss += gross_loss
        total_trades_all += len(all_trades)
        total_wins += len(all_trades[all_trades['PnL'] > 0])

        def safe_metric(pf, key, default=0.0):
            try:
                return pf.stats().get(key, default)
            except:
                return default
        dd = max(safe_metric(pf_long, 'Max Drawdown [%]', 0.0), safe_metric(pf_short, 'Max Drawdown [%]', 0.0))
        if dd > max_dd_overall:
            max_dd_overall = dd

        wr = (len(all_trades[all_trades['PnL'] > 0]) / len(all_trades)) * 100 if len(all_trades) > 0 else 0
        window_pf = gross_profit / gross_loss if gross_loss > 0 else float('inf')
        windows_info.append({
            'window': f"{train_end.date()} - {window_end.date()} (test)",
            'best_params': f"RSI={rsi_best},SW={sw_best},SL={sl_best},TP={tp_best}",
            'trades': len(all_trades),
            'win_rate': wr,
            'pf': window_pf,
            'max_dd': dd
        })
        print(f"Window test {train_end.date()} -> {window_end.date()}: Params={best_params}, Trades={len(all_trades)}, WR={wr:.1f}%, PF={window_pf:.2f}")
        window_start += pd.DateOffset(days=step_days)

    overall_pf = total_gross_profit / total_gross_loss if total_gross_loss > 0 else float('inf')
    overall_wr = (total_wins / total_trades_all * 100) if total_trades_all > 0 else 0
    all_pnl = pd.concat([pf_long.trades.records_readable['PnL'], pf_short.trades.records_readable['PnL']])
    overall_sharpe = all_pnl.mean() / all_pnl.std() * np.sqrt(365*24*4) if all_pnl.std() > 0 else 0.0

    return pd.DataFrame(windows_info), overall_pf, overall_sharpe, max_dd_overall, overall_wr, total_trades_all

if __name__ == "__main__":
    print(f"Downloading last 6 months of {TIMEFRAME} data for {SYMBOL}...")
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

    print("Running walk-forward optimization (grid search)...")
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
        print("\n🚀 Strategy 4 (Optimized) passes all gates! We found the edge.")
    else:
        print("\n⚠️ Optimization completed, but gates not fully passed. Review parameters.")
