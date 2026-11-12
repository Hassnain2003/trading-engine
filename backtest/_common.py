"""Shared data-fetching and trade-simulation helpers for the current-parameter
re-backtests (backtest/relaxed_current_params.py). Kept separate so all three
strategy backtests share one execution-cost model and one trade simulator.
"""

import os
import time

import ccxt
import pandas as pd

DATA_DIR = os.path.join(os.path.dirname(__file__), 'data')

# Cost model: approximates an IOC order crossing the spread (SLIPPAGE) plus
# exchange fees (FEE), applied on both entry and exit — same order of
# magnitude as the SLIPPAGE/COMMISSION constants already used elsewhere in
# backtest/ (e.g. walk_forward.py, s4_btc_optimize.py).
SLIPPAGE = 0.0005
FEE = 0.0002


def fetch_ohlcv_cached(symbol, timeframe, days):
    """Download (or load from local cache) `days` of history for symbol/timeframe."""
    safe_symbol = symbol.replace('/', '-').replace(':', '-')
    path = os.path.join(DATA_DIR, f"{safe_symbol}_{timeframe}_{days}d.parquet")
    if os.path.exists(path):
        return pd.read_parquet(path)

    exchange = ccxt.bybit({'enableRateLimit': True, 'options': {'defaultType': 'linear'}})
    until = exchange.milliseconds()
    since = until - days * 24 * 60 * 60 * 1000
    all_candles = []
    while since < until:
        try:
            candles = exchange.fetch_ohlcv(symbol, timeframe, since=since, limit=1000)
        except Exception as e:
            print(f"    error fetching {symbol} {timeframe}: {e}, retrying...")
            time.sleep(5)
            continue
        if not candles:
            break
        all_candles += candles
        since = candles[-1][0] + 1
        time.sleep(exchange.rateLimit / 1000)
    if not all_candles:
        return pd.DataFrame()

    df = pd.DataFrame(all_candles, columns=['ts', 'open', 'high', 'low', 'close', 'volume'])
    df['ts'] = pd.to_datetime(df['ts'], unit='ms', utc=True)
    df = df.drop_duplicates(subset='ts').set_index('ts').sort_index()
    os.makedirs(DATA_DIR, exist_ok=True)
    df.to_parquet(path)
    print(f"    fetched+cached {len(df)} {timeframe} candles for {symbol}")
    return df


def simulate_trades(df, entries_long, entries_short, sl_long, tp_long, sl_short, tp_short, symbol=""):
    """Bar-by-bar trade simulator: walks forward from each entry until stop-loss
    or take-profit is hit (whichever the bar's high/low touches first; if both
    are touched within the same bar, assumes the worse outcome — SL — which is
    the standard conservative convention). No new entry on a symbol while a
    trade on it is still open, mirroring the live engine's _symbol_busy guard.
    """
    trades = []
    n = len(df)
    highs, lows, closes = df['high'].values, df['low'].values, df['close'].values
    idx = df.index
    i = 0
    while i < n:
        is_long = bool(entries_long.iloc[i]) if entries_long is not None else False
        is_short = bool(entries_short.iloc[i]) if entries_short is not None else False
        if not is_long and not is_short:
            i += 1
            continue
        side = 'long' if is_long else 'short'
        raw_entry = closes[i]
        entry_price = raw_entry * (1 + SLIPPAGE) if side == 'long' else raw_entry * (1 - SLIPPAGE)
        sl = float(sl_long.iloc[i]) if side == 'long' else float(sl_short.iloc[i])
        tp = float(tp_long.iloc[i]) if side == 'long' else float(tp_short.iloc[i])
        if pd.isna(sl) or pd.isna(tp):
            i += 1
            continue

        exit_price, exit_reason, exit_time = None, None, None
        j = i + 1
        while j < n:
            hi, lo = highs[j], lows[j]
            if side == 'long':
                hit_sl, hit_tp = lo <= sl, hi >= tp
            else:
                hit_sl, hit_tp = hi >= sl, lo <= tp
            if hit_sl:
                exit_price, exit_reason = sl, 'sl'
            elif hit_tp:
                exit_price, exit_reason = tp, 'tp'
            if exit_price is not None:
                exit_time = idx[j]
                break
            j += 1

        if exit_price is None:
            exit_price, exit_time, exit_reason = closes[-1], idx[-1], 'eod'

        if side == 'long':
            exit_net = exit_price * (1 - SLIPPAGE)
            pnl_pct = (exit_net - entry_price) / entry_price
        else:
            exit_net = exit_price * (1 + SLIPPAGE)
            pnl_pct = (entry_price - exit_net) / entry_price
        pnl_pct -= FEE * 2

        trades.append({
            'symbol': symbol, 'side': side, 'entry_time': idx[i], 'exit_time': exit_time,
            'entry_price': entry_price, 'exit_price': exit_price,
            'pnl_pct': pnl_pct, 'exit_reason': exit_reason,
        })
        i = (j + 1) if exit_time is not None else (i + 1)

    return pd.DataFrame(trades)


def summarize(trades, label):
    if trades.empty:
        print(f"{label}: 0 trades")
        return {'label': label, 'trades': 0, 'win_rate': None, 'pf': None, 'max_dd_pct': None}

    wins = trades[trades['pnl_pct'] > 0]
    losses = trades[trades['pnl_pct'] <= 0]
    gross_profit = wins['pnl_pct'].sum()
    gross_loss = abs(losses['pnl_pct'].sum())
    pf = (gross_profit / gross_loss) if gross_loss > 0 else float('inf')
    win_rate = len(wins) / len(trades) * 100

    equity = (1 + trades.sort_values('exit_time')['pnl_pct']).cumprod()
    running_max = equity.cummax()
    drawdown = (equity - running_max) / running_max
    max_dd_pct = drawdown.min() * 100

    avg_r = trades['pnl_pct'].mean() * 100
    print(
        f"{label}: {len(trades)} trades | WR {win_rate:.1f}% | PF {pf:.2f} | "
        f"MaxDD {max_dd_pct:.1f}% | avg P&L/trade {avg_r:.3f}%"
    )
    return {
        'label': label, 'trades': len(trades), 'win_rate': win_rate,
        'pf': pf, 'max_dd_pct': max_dd_pct, 'avg_pnl_pct': avg_r,
    }
