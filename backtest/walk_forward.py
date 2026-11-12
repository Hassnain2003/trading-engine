import numpy as np
import pandas as pd
import ccxt
import time
from datetime import datetime, timezone, timedelta
import vectorbt as vbt

# ── Config ─────────────────────────────────────────────────────────────────
COIN        = "BTC/USDT:USDT"
TIMEFRAME   = "1h"
SLIPPAGE    = 0.0005
COMMISSION  = 0.0002

IN_SAMPLE_MONTHS  = 6
OUT_SAMPLE_MONTHS = 2

def fetch_data(symbol, timeframe, since, until):
    exchange = ccxt.bybit({'options': {'defaultType': 'linear'}})
    all_candles = []
    while since < until:
        try:
            candles = exchange.fetch_ohlcv(symbol, timeframe, since=since, limit=1000)
        except Exception as e:
            print(f"Error fetching: {e}, retrying...")
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

def ma_crossover_signals(close, fast=9, slow=21):
    ema_fast = vbt.MA.run(close, window=fast, ewm=True).ma
    ema_slow = vbt.MA.run(close, window=slow, ewm=True).ma
    entries = (ema_fast > ema_slow) & (ema_fast.shift(1) <= ema_slow.shift(1))
    exits = (ema_fast < ema_slow) & (ema_fast.shift(1) >= ema_slow.shift(1))
    return entries, exits

def walk_forward_analysis(df, strategy_func, fast_range, slow_range):
    results = []
    start_date = df.index[0]
    end_date = df.index[-1]
    window_len = pd.DateOffset(months=IN_SAMPLE_MONTHS + OUT_SAMPLE_MONTHS)
    window_start = start_date

    while window_start + window_len <= end_date:
        window_end = window_start + window_len
        train_end = window_start + pd.DateOffset(months=IN_SAMPLE_MONTHS)
        test_start = train_end
        test_end = window_end

        train_mask = (df.index >= window_start) & (df.index < train_end)
        test_mask  = (df.index >= test_start) & (df.index < test_end)
        train_close = df.loc[train_mask, 'close']
        test_close  = df.loc[test_mask, 'close']

        if len(train_close) < 50 or len(test_close) < 50:
            window_start += pd.DateOffset(months=OUT_SAMPLE_MONTHS)
            continue

        best_fast, best_slow = fast_range[0], slow_range[0]
        best_pf = -np.inf
        for fast in fast_range:
            for slow in slow_range:
                if fast >= slow:
                    continue
                entries, exits = strategy_func(train_close, fast, slow)
                pf = vbt.Portfolio.from_signals(
                    train_close, entries, exits,
                    direction='longonly',
                    freq=TIMEFRAME, slippage=SLIPPAGE, fees=COMMISSION
                )
                if pf.total_profit() > 0 and pf.stats()['Profit Factor'] > best_pf:
                    best_pf = pf.stats()['Profit Factor']
                    best_fast, best_slow = fast, slow

        entries, exits = strategy_func(test_close, best_fast, best_slow)
        pf_os = vbt.Portfolio.from_signals(
            test_close, entries, exits,
            direction='longonly',
            freq=TIMEFRAME, slippage=SLIPPAGE, fees=COMMISSION
        )
        stats = pf_os.stats()
        results.append({
            'window': f"{window_start.date()} - {test_end.date()}",
            'fast': best_fast,
            'slow': best_slow,
            'profit_factor': stats['Profit Factor'],
            'sharpe_ratio': stats['Sharpe Ratio'],
            'max_drawdown': stats['Max Drawdown [%]'],   # <-- corrected key
            'win_rate': stats['Win Rate [%]']
        })
        print(f"Window {window_start.date()} -> {test_end.date()}: "
              f"PF={stats['Profit Factor']:.2f}, WR={stats['Win Rate [%]']:.1f}%")

        window_start += pd.DateOffset(months=OUT_SAMPLE_MONTHS)

    return pd.DataFrame(results)

if __name__ == "__main__":
    print("Downloading 12 months of 1h data...")
    end_date = datetime.now(timezone.utc)
    start_date = end_date - timedelta(days=365)

    exchange = ccxt.bybit()
    since_ms = exchange.parse8601(start_date.isoformat())
    until_ms = exchange.parse8601(end_date.isoformat())

    df = fetch_data(COIN, TIMEFRAME, since_ms, until_ms)
    if df.empty:
        print("No data downloaded. Exiting.")
        exit(1)

    print(f"Loaded {len(df)} candles from {df.index[0]} to {df.index[-1]}.\n")

    fast_range = np.arange(5, 31, 5)
    slow_range = np.arange(10, 61, 10)

    results = walk_forward_analysis(df, ma_crossover_signals, fast_range, slow_range)

    if results.empty:
        print("No walk‑forward windows completed. Check data range.")
        exit()

    print("\n========== WALK-FORWARD RESULTS ==========")
    print(results.to_string(index=False))

    avg_pf = results['profit_factor'].mean()
    avg_sharpe = results['sharpe_ratio'].mean()
    avg_dd = results['max_drawdown'].mean()        # already in percent
    avg_wr = results['win_rate'].mean()

    print("\n========== GATE SUMMARY ==========")
    checks = [
        ("Avg Profit Factor > 1.3", avg_pf > 1.3, f"{avg_pf:.2f}"),
        ("Avg Sharpe > 1.0", avg_sharpe > 1.0, f"{avg_sharpe:.2f}"),
        ("Avg Max DD < 20%", avg_dd < 20.0, f"{avg_dd:.2f}%"),
        ("Win Rate 45-62%", 45 <= avg_wr <= 62, f"{avg_wr:.1f}%")
    ]

    for desc, passed, val in checks:
        print(f"{'✅' if passed else '❌'} {desc}: {val}")

    if all(p for _, p, _ in checks):
        print("\n🚀 Strategy 5 passes all gates.")
    else:
        print("\n⚠️ Strategy 5 needs parameter tuning.")
