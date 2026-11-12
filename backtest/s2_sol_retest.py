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
RETEST_CANDLES = 1   # number of confirming candles after breakout
ATR_PER     = 14
ATR_SL_MULT = 1.5
ATR_TP_MULT = 2.0

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
    trend_bullish_5m = trend_bullish.reindex(df.index, method='ffill').fillna(False).infer_objects(copy=False)
    trend_bearish_5m = trend_bearish.reindex(df.index, method='ffill').fillna(False).infer_objects(copy=False)

    high_20 = df['high'].rolling(window=20).max().shift(1)
    low_20 = df['low'].rolling(window=20).min().shift(1)

    avg_vol_20 = df['volume'].rolling(window=20).mean().shift(1)
    vol_ok = df['volume'] > VOL_SPIKE * avg_vol_20

    break_long = (df['close'] > high_20) & vol_ok & trend_bullish_5m
    break_short = (df['close'] < low_20) & vol_ok & trend_bearish_5m

    # Retest: next candle must stay beyond breakout level
    retest_long = break_long.shift(1) & (df['close'] > high_20.shift(1))
    entries_long = retest_long.shift(1)   # enter on candle after retest
    retest_short = break_short.shift(1) & (df['close'] < low_20.shift(1))
    entries_short = retest_short.shift(1)

    # Clean NaN from shifts
    entries_long = entries_long.fillna(False).infer_objects(copy=False)
    entries_short = entries_short.fillna(False).infer_objects(copy=False)

    exit_long = (df['close'] < low_20).fillna(False).infer_objects(copy=False)
    exit_short = (df['close'] > high_20).fillna(False).infer_objects(copy=False)

    atr = vbt.ATR.run(df['high'], df['low'], df['close'], window=ATR_PER).atr
    return entries_long, entries_short, exit_long, exit_short, atr

def walk_forward_analysis(df, window_size_days=60, step_days=30):
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
        test_mask = (df.index >= window_start) & (df.index < window_end)
        test_df = df.loc[test_mask].copy()
        if len(test_df) < 200:
            window_start += pd.DateOffset(days=step_days)
            continue

        entries_l, entries_s, exit_l, exit_s, atr = strategy2_signals(test_df)

        pf_long = vbt.Portfolio.from_signals(
            test_df['close'], entries=entries_l, exits=exit_l,
            sl_stop=test_df['close'] - ATR_SL_MULT * atr,
            tp_stop=test_df['close'] + ATR_TP_MULT * atr,
            direction='longonly', freq=TIMEFRAME, slippage=SLIPPAGE, fees=COMMISSION
        )
        pf_short = vbt.Portfolio.from_signals(
            test_df['close'], entries=entries_s, exits=exit_s,
            sl_stop=test_df['close'] + ATR_SL_MULT * atr,
            tp_stop=test_df['close'] - ATR_TP_MULT * atr,
            direction='shortonly', freq=TIMEFRAME, slippage=SLIPPAGE, fees=COMMISSION
        )

        all_trades = pd.concat([pf_long.trades.records_readable, pf_short.trades.records_readable])
        if len(all_trades) == 0:
            window_start += pd.DateOffset(days=step_days)
            continue

        gross_profit = all_trades[all_trades['PnL'] > 0]['PnL'].sum()
        gross_loss = all_trades[all_trades['PnL'] < 0]['PnL'].sum()
        total_gross_profit += gross_profit
        total_gross_loss += abs(gross_loss)
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
        window_pf = gross_profit / abs(gross_loss) if abs(gross_loss) > 0 else float('inf')
        windows_info.append({
            'window': f"{window_start.date()} - {window_end.date()}",
            'trades': len(all_trades),
            'win_rate': wr,
            'pf': window_pf,
            'max_dd': dd
        })
        print(f"Window {window_start.date()} -> {window_end.date()}: Trades={len(all_trades)}, WR={wr:.1f}%, PF={window_pf:.2f}")
        window_start += pd.DateOffset(days=step_days)

    overall_pf = total_gross_profit / total_gross_loss if total_gross_loss > 0 else float('inf')
    overall_wr = (total_wins / total_trades_all * 100) if total_trades_all > 0 else 0
    all_pnl = pd.concat([pf_long.trades.records_readable['PnL'], pf_short.trades.records_readable['PnL']])
    overall_sharpe = all_pnl.mean() / all_pnl.std() * np.sqrt(365*24*12) if all_pnl.std() > 0 else 0.0

    return pd.DataFrame(windows_info), overall_pf, overall_sharpe, max_dd_overall, overall_wr, total_trades_all

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

    windows_df, overall_pf, overall_sharpe, max_dd, overall_wr, total_trades = walk_forward_analysis(df)

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
        print("\n🚀 Strategy 2 with retest passes all gates!")
    else:
        print("\n⚠️ Needs refinement, but the retest is now alive.")
