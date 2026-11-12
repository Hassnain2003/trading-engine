import numpy as np
import pandas as pd
import pandas_ta as ta
import ccxt
import time
from datetime import datetime, timezone, timedelta
import vectorbt as vbt

SYMBOL      = "ETH/USDT:USDT"
TIMEFRAME   = "5m"
SLIPPAGE    = 0.0005
COMMISSION  = 0.0002
VWAP_STDEV  = 2.0
RSI_PER     = 5
RSI_OS      = 30
RSI_OB      = 70
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

def strategy3_signals(df):
    typical = (df['high'] + df['low'] + df['close']) / 3
    cum_vol = df['volume'].cumsum()
    cum_vp  = (typical * df['volume']).cumsum()
    vwap = cum_vp / cum_vol
    vwap_std = (df['close'] - vwap).rolling(window=len(df), min_periods=1).std()
    upper = vwap + VWAP_STDEV * vwap_std
    lower = vwap - VWAP_STDEV * vwap_std
    
    rsi = vbt.RSI.run(df['close'], window=RSI_PER).rsi
    bull_engulf = (df['close'] > df['open']) & (df['close'].shift(1) < df['open'].shift(1))
    bear_engulf = (df['close'] < df['open']) & (df['close'].shift(1) > df['open'].shift(1))
    avg_vol = df['volume'].rolling(window=20).mean().shift(1)
    vol_spike = df['volume'] > avg_vol
    
    entries_long = (df['close'] <= lower) & (rsi < RSI_OS) & bull_engulf & vol_spike
    entries_short = (df['close'] >= upper) & (rsi > RSI_OB) & bear_engulf & vol_spike
    
    exit_long = df['close'] >= vwap
    exit_short = df['close'] <= vwap
    atr = vbt.ATR.run(df['high'], df['low'], df['close'], window=ATR_PER).atr
    
    return entries_long, entries_short, exit_long, exit_short, atr

def walk_forward_analysis(df, window_size_days=30, step_days=15):
    results = []
    start_date = df.index[0]
    end_date = df.index[-1]
    window_start = start_date
    while window_start + pd.DateOffset(days=window_size_days) <= end_date:
        window_end = window_start + pd.DateOffset(days=window_size_days)
        test_mask = (df.index >= window_start) & (df.index < window_end)
        test_df = df.loc[test_mask].copy()
        if len(test_df) < 200:
            window_start += pd.DateOffset(days=step_days)
            continue
        
        entries_l, entries_s, exit_l, exit_s, atr = strategy3_signals(test_df)
        
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
        
        def safe_metric(pf, key, default=0.0):
            try:
                return pf.stats().get(key, default)
            except:
                return default
        
        # Combine two sides: average profit factor (or weighted) – we'll compute combined PF as max of the two? 
        # Better: use the portfolio's total gross profit/loss, but since we don't have those keys, we'll use the actual Profit Factor from each side and average them.
        pf_l = safe_metric(pf_long, 'Profit Factor', 0.0)
        pf_s = safe_metric(pf_short, 'Profit Factor', 0.0)
        trades_l = safe_metric(pf_long, 'Total Trades', 0)
        trades_s = safe_metric(pf_short, 'Total Trades', 0)
        total_trades = trades_l + trades_s
        if total_trades > 0:
            combined_pf = (pf_l * trades_l + pf_s * trades_s) / total_trades
        else:
            combined_pf = 0.0
        
        win_l = safe_metric(pf_long, 'Win Rate [%]', 0.0)
        win_s = safe_metric(pf_short, 'Win Rate [%]', 0.0)
        combined_wr = (win_l * trades_l + win_s * trades_s) / total_trades if total_trades > 0 else 0.0
        
        max_dd_l = safe_metric(pf_long, 'Max Drawdown [%]', 0.0)
        max_dd_s = safe_metric(pf_short, 'Max Drawdown [%]', 0.0)
        max_dd = max(max_dd_l, max_dd_s)
        
        sharpe_l = safe_metric(pf_long, 'Sharpe Ratio', 0.0)
        sharpe_s = safe_metric(pf_short, 'Sharpe Ratio', 0.0)
        sharpe = (sharpe_l + sharpe_s) / 2
        
        results.append({
            'window': f"{window_start.date()} - {window_end.date()}",
            'profit_factor': combined_pf,
            'sharpe_ratio': sharpe,
            'max_drawdown': max_dd,
            'win_rate': combined_wr,
            'trades': total_trades
        })
        print(f"Window {window_start.date()} -> {window_end.date()}: "
              f"PF={combined_pf:.2f}, WR={combined_wr:.1f}%, Trades={total_trades}")
        window_start += pd.DateOffset(days=step_days)
    return pd.DataFrame(results)

if __name__ == "__main__":
    print(f"Downloading last 3 months of {TIMEFRAME} data for {SYMBOL}...")
    end_date = datetime.now(timezone.utc)
    start_date = end_date - timedelta(days=90)
    exchange = ccxt.bybit()
    since_ms = exchange.parse8601(start_date.isoformat())
    until_ms = exchange.parse8601(end_date.isoformat())
    df = fetch_data(SYMBOL, TIMEFRAME, since_ms, until_ms)
    if df.empty:
        print("No data. Exit.")
        exit(1)
    print(f"Loaded {len(df)} candles from {df.index[0]} to {df.index[-1]}.\n")
    res = walk_forward_analysis(df, window_size_days=30, step_days=15)
    if res.empty:
        print("No windows completed.")
        exit()
    print("\n========== WALK-FORWARD RESULTS ==========")
    print(res.to_string(index=False))
    avg_pf = res['profit_factor'].mean()
    avg_sharpe = res['sharpe_ratio'].mean()
    avg_dd = res['max_drawdown'].mean()
    avg_wr = res['win_rate'].mean()
    total_trades = res['trades'].sum()
    print(f"\nTotal trades across all windows: {total_trades}")
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
        print("\n🚀 Strategy 3 passes all gates on 5m (3-month test).")
    else:
        print("\n⚠️ Strategy 3 needs parameter tuning.")
