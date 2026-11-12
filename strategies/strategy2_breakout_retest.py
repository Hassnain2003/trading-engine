import ccxt
import os
import pandas as pd
from dotenv import load_dotenv
from datetime import datetime, timezone, timedelta

try:
    import pandas_ta as ta
except ImportError:  # pandas_ta/numba unavailable on Python 3.14; real lib used on CI (Python 3.12).
    ta = None

load_dotenv()

# ── Constants ────────────────────────────────────────────────────────────────
# Live universe: SOL dropped 2026-07-19; BNB paused 2026-08-02 (live −3.3%
# sum / stop-overruns; BTC alone +4.6%).
COINS         = ["BTC/USDT:USDT"]

# Tier A 2026-07-22: live shorts lost ~61% (sum -7.3%); longs 64% WR / +9.1%.
# Keep short logic in the bearish block commented-path below disabled via flag.
LONG_ONLY       = True
# Live losses often overran planned stops (~2R). Floor keeps noise from
# placing 0.5–0.6% stops that demo books routinely blow through.
MIN_STOP_PCT    = 0.012    # 1.2% minimum stop distance from entry
# Live hour buckets: UTC 0–2 and ~11 were net red; keep 12–17 greens.
BLOCKED_UTC_HOURS = frozenset({0, 1, 2, 11})

# Strategy 2 specific
OPENING_MINUTES = 15       # first 15 minutes of the session
VOL_SPIKE_MULT  = 1.5      # breakout volume must be > 1.5x average
RETEST_ZONE_PCT = 0.01       # ±1% retest zone
EMA_FAST        = 9        # 1h EMA fast period
EMA_MEDIUM      = 21       # 1h EMA medium period
EMA_SLOW        = 50       # 1h EMA slow period (used as trend filter)
# 6-month walk-forward (rolling 90/30-day folds, R chosen only on train data)
# showed the old 1.5x target had negative expectancy (PF 0.96) while 2.0x was
# out-of-sample PF 1.17 on SOL+BNB — wider target, not more signals, was the
# fix. BTC validated separately (OOS PF 1.61) with both folds choosing 3.0x,
# hence the per-symbol override. See backtest/s2_r_multiple_walkforward.py
# and backtest/s2_breadth_test.py. ETH/XRP/LINK/DOGE tested negative — do not
# add symbols here without walk-forward evidence.
TP_R_MULTIPLE   = 2.0
TP_R_MULTIPLES  = {"BTC/USDT:USDT": 3.0}

# ── Exchange connection ───────────────────────────────────────────────────────
def connect():
    public = ccxt.bybit({'options': {'defaultType': 'linear'}})

    demo = ccxt.bybit({
        'apiKey': os.getenv('BYBIT_API_KEY'),
        'secret': os.getenv('BYBIT_API_SECRET'),
        'options': {'defaultType': 'linear'},
    })
    demo.urls['api'] = {
        'public':  'https://api-demo.bybit.com',
        'private': 'https://api-demo.bybit.com',
    }
    return public, demo

# ── Fetch OHLCV and build DataFrame ──────────────────────────────────────────
def fetch_candles(exchange, symbol, timeframe, limit=100):
    raw = exchange.fetch_ohlcv(symbol, timeframe, limit=limit)
    df  = pd.DataFrame(raw, columns=['ts','open','high','low','close','volume'])
    df['ts'] = pd.to_datetime(df['ts'], unit='ms', utc=True)
    df.set_index('ts', inplace=True)
    return df


def _apply_min_stop(side, entry, stop_loss, tp_r_multiple):
    """Widen stop to MIN_STOP_PCT if the structure stop is tighter; rebuild TP in R."""
    entry = float(entry)
    stop_loss = float(stop_loss)
    min_dist = entry * MIN_STOP_PCT
    if side == 'long':
        if entry - stop_loss < min_dist:
            stop_loss = entry - min_dist
        risk = max(entry - stop_loss, min_dist)
        take_profit = entry + tp_r_multiple * risk
    else:
        if stop_loss - entry < min_dist:
            stop_loss = entry + min_dist
        risk = max(stop_loss - entry, min_dist)
        take_profit = entry - tp_r_multiple * risk
    return round(stop_loss, 4), round(take_profit, 4)


def strategy2_signal(exchange, symbol):
    """
    Volume-Confirmed Breakout Retest
    - Identify the session opening 15-min range high/low.
    - Wait for a breakout of that range with volume > 1.5x average.
    - Enter on a retest of the broken level (price comes back to test it).
    - Direction must align with 1h EMA ribbon trend.
    - DEMO MODE: uses marketable IOC order so signals actually fill.
    """

    # 1. Determine session open range (based on UTC midnight, like Bybit daily reset)
    now = datetime.now(timezone.utc)
    if now.hour in BLOCKED_UTC_HOURS:
        return None, f"Hour filter — no S2 entries at UTC {now.hour:02d}:xx (blocked {sorted(BLOCKED_UTC_HOURS)})"

    start_of_day = now.replace(hour=0, minute=0, second=0, microsecond=0)
    since = exchange.parse8601(start_of_day.isoformat())
    raw_5m = exchange.fetch_ohlcv(symbol, '5m', since=since, limit=300)
    if not raw_5m:
        return None, "No 5m data today"
    df_5m_today = pd.DataFrame(raw_5m, columns=['ts','open','high','low','close','volume'])
    df_5m_today['ts'] = pd.to_datetime(df_5m_today['ts'], unit='ms', utc=True)
    df_5m_today.set_index('ts', inplace=True)

    first_15m_end = start_of_day + timedelta(minutes=OPENING_MINUTES)
    opening_range = df_5m_today[df_5m_today.index <= first_15m_end]
    if opening_range.empty:
        return None, "Opening range not formed yet"
    session_high = opening_range['high'].max()
    session_low  = opening_range['low'].min()

    # 2. Trend context from 1h EMA ribbon
    df_1h = fetch_candles(exchange, symbol, '1h', limit=50)
    ema_fast   = ta.ema(df_1h['close'], length=EMA_FAST)
    ema_medium = ta.ema(df_1h['close'], length=EMA_MEDIUM)
    ema_slow   = ta.ema(df_1h['close'], length=EMA_SLOW)

    last_fast   = ema_fast.iloc[-1]
    last_medium = ema_medium.iloc[-1]
    last_slow   = ema_slow.iloc[-1]

    trend_bullish = (last_fast > last_medium) and (last_medium > last_slow)
    trend_bearish = (last_fast < last_medium) and (last_medium < last_slow)
    if not (trend_bullish or trend_bearish):
        return None, "EMA ribbon not aligned — no clear trend"

    # 3. Get average volume for volume spike filter
    recent_5m = fetch_candles(exchange, symbol, '5m', limit=25)
    avg_vol = recent_5m['volume'].iloc[:-2].mean()  # exclude current candle

    current_price = df_5m_today['close'].iloc[-1]
    signal = None

    # --- Bullish scenario: price broke above session_high, now retesting it as support ---
    if trend_bullish:
        for i in range(len(df_5m_today)-1, 0, -1):
            candle = df_5m_today.iloc[i]
            if candle['close'] > session_high and candle['volume'] >= VOL_SPIKE_MULT * avg_vol:
                if current_price > session_high * (1 - RETEST_ZONE_PCT) and current_price < session_high * (1 + RETEST_ZONE_PCT):
                    last_candles = df_5m_today.iloc[-3:-1]
                    if all(last_candles['close'] > session_high):
                        stop_loss = session_high - (session_high * 0.005)
                        tp_r = TP_R_MULTIPLES.get(symbol, TP_R_MULTIPLE)
                        stop_loss, target = _apply_min_stop('long', current_price, stop_loss, tp_r)
                        signal = {
                            'symbol':    symbol,
                            'side':      'long',
                            'price':     current_price,
                            'stop_loss': stop_loss,
                            'take_profit': target,
                            'session_high': session_high,
                            'session_low': session_low,
                            'breakout_candle_vol': candle['volume'],
                            'avg_vol': round(avg_vol, 2),
                            'trend': 'bullish',
                            'reason': f"Breakout above {session_high:.4f}, retest holds, IOC marketable entry",
                            'strategy':  'S2_Breakout_Retest',
                            'order_type': 'ioc',   # DEMO: immediate fill
                            'time':      datetime.now(timezone.utc).isoformat(),
                        }
                        break

    # --- Bearish scenario: price broke below session_low, now retesting it as resistance ---
    if trend_bearish and not LONG_ONLY:
        for i in range(len(df_5m_today)-1, 0, -1):
            candle = df_5m_today.iloc[i]
            if candle['close'] < session_low and candle['volume'] >= VOL_SPIKE_MULT * avg_vol:
                if current_price > session_low * (1 - RETEST_ZONE_PCT) and current_price < session_low * (1 + RETEST_ZONE_PCT):
                    last_candles = df_5m_today.iloc[-3:-1]
                    if all(last_candles['close'] < session_low):
                        stop_loss = session_low + (session_low * 0.005)
                        tp_r = TP_R_MULTIPLES.get(symbol, TP_R_MULTIPLE)
                        stop_loss, target = _apply_min_stop('short', current_price, stop_loss, tp_r)
                        signal = {
                            'symbol':    symbol,
                            'side':      'short',
                            'price':     current_price,
                            'stop_loss': stop_loss,
                            'take_profit': target,
                            'session_high': session_high,
                            'session_low': session_low,
                            'breakout_candle_vol': candle['volume'],
                            'avg_vol': round(avg_vol, 2),
                            'trend': 'bearish',
                            'reason': f"Breakout below {session_low:.4f}, retest holds, IOC marketable entry",
                            'strategy':  'S2_Breakout_Retest',
                            'order_type': 'ioc',   # DEMO: immediate fill
                            'time':      datetime.now(timezone.utc).isoformat(),
                        }
                        break

    if signal:
        return signal, "✅ SIGNAL FOUND"
    if LONG_ONLY and trend_bearish:
        return None, "Long-only mode — bearish breakout skipped"
    return None, f"No valid retest near level (range: {session_low:.4f}-{session_high:.4f})"

def run_scanner(public_exchange):
    from utils.gate import check_liquidity

    print(f"\n{'='*65}")
    print(f"  STRATEGY 2 — Volume Breakout Retest Scan")
    print(f"  {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')} UTC")
    print(f"{'='*65}")

    for symbol in COINS:
        coin = symbol.split('/')[0]
        print(f"\n── {coin} ──────────────────────────────────────")

        gate_ok, gate_reason = check_liquidity(public_exchange, symbol)
        if not gate_ok:
            print(f"  ❌ GATE FAIL: {gate_reason}")
            continue
        print(f"  ✅ Gate passed: {gate_reason}")

        signal, msg = strategy2_signal(public_exchange, symbol)
        print(f"  Signal: {msg}")

        if signal:
            print(f"\n  🚨 TRADE SIGNAL 🚨")
            print(f"  Side:        {signal['side'].upper()}")
            print(f"  Entry:       ${signal['price']:,.4f}")
            print(f"  Stop Loss:   ${signal['stop_loss']:,.4f}")
            print(f"  Take Profit: ${signal['take_profit']:,.4f}")
            print(f"  Breakout level: {signal.get('session_high' if signal['side']=='long' else 'session_low'):,.4f}")
            print(f"  Volume:      {signal['breakout_candle_vol']:.0f} vs avg {signal['avg_vol']:.0f}")
            print(f"  Trend:       {signal['trend']}")
            print(f"  Reason:      {signal['reason']}")

if __name__ == "__main__":
    print("Connecting...")
    public, demo = connect()
    print("Connected. Scanning for Strategy 2 signals...")
    run_scanner(public)
