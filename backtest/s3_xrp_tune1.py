import numpy as np
import pandas as pd
import pandas_ta as ta
import ccxt
import time
from datetime import datetime, timezone, timedelta
import vectorbt as vbt

SYMBOL      = "XRP/USDT:USDT"
TIMEFRAME   = "5m"
SLIPPAGE    = 0.001
COMMISSION  = 0.0002
VWAP_STDEV  = 2.0
RSI_PER     = 5
RSI_OS      = 30
RSI_OB      = 70
ATR_PER     = 14
ATR_SL_MULT = 2.0   # <--- changed from 1.5 to 2.0
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
        windows_info.append({'window': f"{window_start.date()} - {window_end.date()}", 'trades': len(all_trades), 'win_rate': wr, 'pf': window_pf, 'max_dd': dd})
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
        ("Minimum 60 Trades", total_trades >= 60, f"{total_trades}")
    ]
    for desc, passed, val in checks:
        print(f"{'✅' if passed else '❌'} {desc}: {val}")
    if all(p for _, p, _ in checks):
        print("\n🚀 Strategy 3 (XRP, 2x ATR stop) passes all gates!")
    else:
        print("\n⚠️ Still needs further tuning.")
