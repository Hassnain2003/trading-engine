import numpy as np
import pandas as pd
import pandas_ta as ta
import ccxt
import time
from datetime import datetime, timezone, timedelta
import vectorbt as vbt

# ── Config ─────────────────────────────────────────────────────────────────
SYMBOL      = "BTC/USDT:USDT"
TIMEFRAME   = "1h"          # 1-hour for fast backtests
SLIPPAGE    = 0.0005
COMMISSION  = 0.0002
RSI_PER     = 14
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
    # All indicators on the 1h dataframe
    rsi = vbt.RSI.run(df['close'], window=RSI_PER).rsi
    adx = ta.adx(df['high'], df['low'], df['close'], length=ADX_PER)[f'ADX_{ADX_PER}']
    atr = vbt.ATR.run(df['high'], df['low'], df['close'], window=ATR_PER).atr

    range_market = adx < ADX_MAX

    # Long: RSI oversold and crosses above 30
    os = rsi < RSI_OS
    cross_up = (rsi > RSI_OS) & (rsi.shift(1) <= RSI_OS)
    entries_long = range_market & os & cross_up

    # Short: RSI overbought and crosses below 70
    ob = rsi > RSI_OB
    cross_down = (rsi < RSI_OB) & (rsi.shift(1) >= RSI_OB)
    entries_short = range_market & ob & cross_down

    # Exit when RSI returns to 50
    exit_long = rsi > 50
    exit_short = rsi < 50

    return entries_long, entries_short, exit_long, exit_short, atr

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

        test_mask = (df.index >= test_start) & (df.index < test_end)
        test_df = df.loc[test_mask].copy()

        if len(test_df) < 20:
            window_start += pd.DateOffset(months=OUT_SAMPLE_MONTHS)
            continue

        entries_l, entries_s, exit_l, exit_s, atr = strategy1_signals(test_df)

        # Run portfolios
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

        # Safe metric extraction (use 'Total Profit' and 'Total Loss')
        def get_metric(pf, key, default=0.0):
            try:
                return pf.stats()[key]
            except KeyError:
                return default

        total_profit_l = get_metric(pf_long, 'Total Profit')
        total_profit_s = get_metric(pf_short, 'Total Profit')
        total_loss_l = get_metric(pf_long, 'Total Loss')
        total_loss_s = get_metric(pf_short, 'Total Loss')
        gross_profit = total_profit_l + total_profit_s
        gross_loss = abs(total_loss_l) + abs(total_loss_s)
        combined_pf = gross_profit / gross_loss if gross_loss > 0 else 0.0

        trades_l = get_metric(pf_long, 'Total Trades', 0)
        trades_s = get_metric(pf_short, 'Total Trades', 0)
        total_trades = trades_l + trades_s
        if total_trades > 0:
            wr_l = get_metric(pf_long, 'Win Rate [%]', 0.0)
            wr_s = get_metric(pf_short, 'Win Rate [%]', 0.0)
            combined_wr = (trades_l * wr_l + trades_s * wr_s) / total_trades
        else:
            combined_wr = 0.0

        max_dd = max(get_metric(pf_long, 'Max Drawdown [%]', 0.0),
                     get_metric(pf_short, 'Max Drawdown [%]', 0.0))
        avg_sharpe = (get_metric(pf_long, 'Sharpe Ratio', 0.0) + get_metric(pf_short, 'Sharpe Ratio', 0.0)) / 2

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
        print("\n🚀 Strategy 1 (1h fast test) passes all gates!")
    else:
        print("\n⚠️ Strategy 1 (1h fast test) needs parameter tuning.")
