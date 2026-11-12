import numpy as np
import pandas as pd
import pandas_ta as ta
import ccxt
import time
from datetime import datetime, timezone, timedelta
import vectorbt as vbt

# ── Config ─────────────────────────────────────────────────────────────────
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

IN_SAMPLE_MONTHS  = 4
OUT_SAMPLE_MONTHS = 2

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
    # 1. Resample to 1h for context
    df_1h = df.resample('1h').agg({
        'open': 'first', 'high': 'max', 'low': 'min', 'close': 'last', 'volume': 'sum'
    }).dropna()
    
    # 2. Compute 1h indicators using pandas_ta (ADX) and vectorbt (RSI, ATR)
    rsi_1h = vbt.RSI.run(df_1h['close'], window=RSI_1H_PER).rsi
    adx_1h = ta.adx(df_1h['high'], df_1h['low'], df_1h['close'], length=ADX_PER)
    adx_1h = adx_1h[f'ADX_{ADX_PER}']   # extract the ADX line
    atr_1h = vbt.ATR.run(df_1h['high'], df_1h['low'], df_1h['close'], window=ATR_PER).atr
    
    # 3. Align to 5-minute index (forward fill)
    rsi_1h_5m = rsi_1h.reindex(df.index, method='ffill')
    adx_1h_5m = adx_1h.reindex(df.index, method='ffill')
    atr_1h_5m = atr_1h.reindex(df.index, method='ffill')
    
    # 4. 5-minute RSI
    rsi_5m = vbt.RSI.run(df['close'], window=RSI_5M_PER).rsi
    
    # 5. Signal conditions
    range_market = adx_1h_5m < ADX_MAX
    
    # Long: 1h oversold AND 5m crosses above 30
    os_1h = rsi_1h_5m < RSI_OS
    cross_up = (rsi_5m > RSI_OS) & (rsi_5m.shift(1) <= RSI_OS)
    entries_long = range_market & os_1h & cross_up
    
    # Short: 1h overbought AND 5m crosses below 70
    ob_1h = rsi_1h_5m > RSI_OB
    cross_down = (rsi_5m < RSI_OB) & (rsi_5m.shift(1) >= RSI_OB)
    entries_short = range_market & ob_1h & cross_down
    
    # Exit when RSI(5) returns to 50
    exit_long = rsi_5m > 50
    exit_short = rsi_5m < 50
    
    return entries_long, entries_short, exit_long, exit_short, atr_1h_5m

def walk_forward_analysis(df):
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
        
        test_mask  = (df.index >= test_start) & (df.index < test_end)
        test_df  = df.loc[test_mask].copy()
        
        if len(test_df) < 200:
            window_start += pd.DateOffset(months=OUT_SAMPLE_MONTHS)
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
        
        gross_profit = pf_long.stats()['Gross Profit'] + pf_short.stats()['Gross Profit']
        gross_loss = abs(pf_long.stats()['Gross Loss']) + abs(pf_short.stats()['Gross Loss'])
        combined_pf = gross_profit / gross_loss if gross_loss > 0 else 0.0
        
        total_trades_l = pf_long.stats()['Total Trades']
        total_trades_s = pf_short.stats()['Total Trades']
        total_trades = total_trades_l + total_trades_s
        if total_trades > 0:
            combined_wr = (total_trades_l * pf_long.stats()['Win Rate [%]'] + 
                           total_trades_s * pf_short.stats()['Win Rate [%]']) / total_trades
        else:
            combined_wr = 0.0
        
        max_dd = max(pf_long.stats()['Max Drawdown [%]'], pf_short.stats()['Max Drawdown [%]'])
        avg_sharpe = (pf_long.stats()['Sharpe Ratio'] + pf_short.stats()['Sharpe Ratio']) / 2
        
        results.append({
            'window': f"{window_start.date()} - {test_end.date()}",
            'profit_factor': combined_pf,
            'sharpe_ratio': avg_sharpe,
            'max_drawdown': max_dd,
            'win_rate': combined_wr
        })
        print(f"Window {window_start.date()} -> {test_end.date()}: "
              f"PF={combined_pf:.2f}, WR={combined_wr:.1f}%")
        
        window_start += pd.DateOffset(months=OUT_SAMPLE_MONTHS)
    
    return pd.DataFrame(results)

if __name__ == "__main__":
    print(f"Downloading 12 months of {TIMEFRAME} data for {SYMBOL}...")
    end_date = datetime.now(timezone.utc)
    start_date = end_date - timedelta(days=365)
    
    exchange = ccxt.bybit()
    since_ms = exchange.parse8601(start_date.isoformat())
    until_ms = exchange.parse8601(end_date.isoformat())
    
    df = fetch_data(SYMBOL, TIMEFRAME, since_ms, until_ms)
    if df.empty:
        print("No data. Exit.")
        exit(1)
    print(f"Loaded {len(df)} candles from {df.index[0]} to {df.index[-1]}.\n")
    
    res = walk_forward_analysis(df)
    if res.empty:
        print("No windows completed.")
        exit()
    
    print("\n========== WALK-FORWARD RESULTS ==========")
    print(res.to_string(index=False))
    
    avg_pf = res['profit_factor'].mean()
    avg_sharpe = res['sharpe_ratio'].mean()
    avg_dd = res['max_drawdown'].mean()
    avg_wr = res['win_rate'].mean()
    
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
        print("\n🚀 Strategy 1 passes all gates!")
    else:
        print("\n⚠️ Strategy 1 needs parameter tuning.")
