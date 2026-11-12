import numpy as np
import pandas as pd
import pandas_ta as ta
import ccxt
import time
from datetime import datetime, timezone, timedelta
import vectorbt as vbt

SYMBOL      = "BTC/USDT:USDT"
TIMEFRAME   = "5m"
SLIPPAGE    = 0.0005
COMMISSION  = 0.0002
RSI_1H_PER  = 14
RSI_5M_PER  = 7
RSI_OS      = 30
RSI_OB      = 70
ADX_PER     = 14
ADX_MAX     = 25
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

def strategy1_signals(df):
    # 1h context from resampling
    df_1h = df.resample('1h').agg({
        'open': 'first', 'high': 'max', 'low': 'min', 'close': 'last', 'volume': 'sum'
    }).dropna()
    
    rsi_1h = vbt.RSI.run(df_1h['close'], window=RSI_1H_PER).rsi
    adx_1h = ta.adx(df_1h['high'], df_1h['low'], df_1h['close'], length=ADX_PER)[f'ADX_{ADX_PER}']
    atr_1h = vbt.ATR.run(df_1h['high'], df_1h['low'], df_1h['close'], window=ATR_PER).atr
    
    rsi_1h_5m = rsi_1h.reindex(df.index, method='ffill')
    adx_1h_5m = adx_1h.reindex(df.index, method='ffill')
    atr_1h_5m = atr_1h.reindex(df.index, method='ffill')
    
    rsi_5m = vbt.RSI.run(df['close'], window=RSI_5M_PER).rsi
    
    range_market = adx_1h_5m < ADX_MAX
    
    os_1h = rsi_1h_5m < RSI_OS
    cross_up = (rsi_5m > RSI_OS) & (rsi_5m.shift(1) <= RSI_OS)
    entries_long = range_market & os_1h & cross_up
    
    ob_1h = rsi_1h_5m > RSI_OB
    cross_down = (rsi_5m < RSI_OB) & (rsi_5m.shift(1) >= RSI_OB)
    entries_short = range_market & ob_1h & cross_down
    
    exit_long = rsi_5m > 50
    exit_short = rsi_5m < 50
    
    return entries_long, entries_short, exit_long, exit_short, atr_1h_5m

def walk_forward_analysis(df, window_size_days=60, step_days=20):
    results = []
    start_date = df.index[0]
    end_date = df.index[-1]
    window_start = start_date
    while window_start + pd.DateOffset(days=window_size_days) <= end_date:
        window_end = window_start + pd.DateOffset(days=window_size_days)
        test_end = window_end
        test_start = window_start
        test_mask = (df.index >= test_start) & (df.index < test_end)
        test_df = df.loc[test_mask].copy()
        if len(test_df) < 200:
            window_start += pd.DateOffset(days=step_days)
            continue
        
        entries_l, entries_s, exit_l, exit_s, atr = strategy1_signals(test_df)
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
                return pf.stats()[key]
            except KeyError:
                return default
        
        total_profit = safe_metric(pf_long, 'Total Profit') + safe_metric(pf_short, 'Total Profit')
        total_loss = abs(safe_metric(pf_long, 'Total Loss')) + abs(safe_metric(pf_short, 'Total Loss'))
        combined_pf = total_profit / total_loss if total_loss > 0 else 0.0
        
        trades_l = safe_metric(pf_long, 'Total Trades', 0)
        trades_s = safe_metric(pf_short, 'Total Trades', 0)
        total_trades = trades_l + trades_s
        if total_trades > 0:
            wr = (trades_l * safe_metric(pf_long, 'Win Rate [%]', 0.0) + 
                  trades_s * safe_metric(pf_short, 'Win Rate [%]', 0.0)) / total_trades
        else:
            wr = 0.0
        
        max_dd = max(safe_metric(pf_long, 'Max Drawdown [%]', 0.0), safe_metric(pf_short, 'Max Drawdown [%]', 0.0))
        sharpe = (safe_metric(pf_long, 'Sharpe Ratio', 0.0) + safe_metric(pf_short, 'Sharpe Ratio', 0.0)) / 2
        
        results.append({
            'window': f"{test_start.date()} - {test_end.date()}",
            'profit_factor': combined_pf,
            'sharpe_ratio': sharpe,
            'max_drawdown': max_dd,
            'win_rate': wr,
            'trades': total_trades
        })
        print(f"Window {test_start.date()} -> {test_end.date()}: "
              f"PF={combined_pf:.2f}, WR={wr:.1f}%, Trades={total_trades}")
        window_start += pd.DateOffset(days=step_days)
    return pd.DataFrame(results)

if __name__ == "__main__":
    print(f"Downloading last 3 months of {TIMEFRAME} data for {SYMBOL}...")
    end_date = datetime.now(timezone.utc)
    start_date = end_date - timedelta(days=90)   # 3 months only
    exchange = ccxt.bybit()
    since_ms = exchange.parse8601(start_date.isoformat())
    until_ms = exchange.parse8601(end_date.isoformat())
    df = fetch_data(SYMBOL, TIMEFRAME, since_ms, until_ms)
    if df.empty:
        print("No data. Exit.")
        exit(1)
    print(f"Loaded {len(df)} candles from {df.index[0]} to {df.index[-1]}.\n")
    res = walk_forward_analysis(df, window_size_days=30, step_days=15)  # shorter windows to get more data points
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
        print("\n🚀 Strategy 1 passes all gates on 5m (3-month test).")
    else:
        print("\n⚠️ Strategy 1 needs parameter tuning.")
