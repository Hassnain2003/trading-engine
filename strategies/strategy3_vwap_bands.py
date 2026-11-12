import ccxt
import os
import pandas as pd
import pandas_ta as ta
from dotenv import load_dotenv
from datetime import datetime, timezone

load_dotenv()

# ── Constants ────────────────────────────────────────────────────────────────
COINS         = ["XRP/USDT:USDT", "ETH/USDT:USDT"]

VWAP_STDEV    = 2.0
RSI_PERIOD    = 5
RSI_OVERSOLD  = 35          # DEMO relaxed
RSI_OVERBOUGHT = 65         # DEMO relaxed
ADX_PERIOD    = 14
ADX_MAX = 60                # DEMO: only skip extreme trends

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

# ── Fetch OHLCV ──────────────────────────────────────────────────────────────
def fetch_candles(exchange, symbol, timeframe, limit=100):
    raw = exchange.fetch_ohlcv(symbol, timeframe, limit=limit)
    df = pd.DataFrame(raw, columns=['ts','open','high','low','close','volume'])
    df['ts'] = pd.to_datetime(df['ts'], unit='ms', utc=True)
    df.set_index('ts', inplace=True)
    return df

# ── Strategy 3: VWAP Standard Deviation Bands (Mean Reversion) ────────────────
def strategy3_signal(exchange, symbol):
    """
    VWAP ±2σ Bands – DEMO RELAXED
    Enter when price touches band AND RSI is beyond threshold.
    No engulfing or volume spike required for demo testing.
    """
    now = datetime.now(timezone.utc)
    start_of_day = now.replace(hour=0, minute=0, second=0, microsecond=0)
    since = exchange.parse8601(start_of_day.isoformat())
    raw = exchange.fetch_ohlcv(symbol, '5m', since=since, limit=300)
    if not raw:
        return None, "No intraday data"
    df = pd.DataFrame(raw, columns=['ts','open','high','low','close','volume'])
    df['ts'] = pd.to_datetime(df['ts'], unit='ms', utc=True)
    df.set_index('ts', inplace=True)
    if len(df) < 30:
        return None, "Not enough intraday data for VWAP"

    # Regime filter: only skip if ADX extremely high
    df_1h = fetch_candles(exchange, symbol, '1h', limit=30)
    adx_1h = ta.adx(df_1h['high'], df_1h['low'], df_1h['close'], length=ADX_PERIOD)
    last_adx = float(adx_1h[f'ADX_{ADX_PERIOD}'].iloc[-1])
    if last_adx > ADX_MAX:
        return None, f"ADX {last_adx:.1f} > {ADX_MAX} — trending, skip mean reversion"

    # Calculate VWAP and standard deviation bands
    typical_price = (df['high'] + df['low'] + df['close']) / 3
    df['cum_vol'] = df['volume'].cumsum()
    df['cum_vp']  = (typical_price * df['volume']).cumsum()
    df['vwap']    = df['cum_vp'] / df['cum_vol']
    df['vwap_std'] = df['close'].rolling(window=len(df), min_periods=1).std()
    df['upper_band'] = df['vwap'] + VWAP_STDEV * df['vwap_std']
    df['lower_band'] = df['vwap'] - VWAP_STDEV * df['vwap_std']

    # RSI(5)
    df['rsi'] = ta.rsi(df['close'], length=RSI_PERIOD)

    current = df.iloc[-1]
    signal = None

    # ── Long setup: price at/below lower band AND RSI oversold ──────────────
    if current['close'] <= current['lower_band'] and current['rsi'] < RSI_OVERSOLD:
        stop_loss = current['lower_band'] * 0.9985
        target = current['vwap']
        risk = current['close'] - stop_loss
        if risk > 0:
            signal = {
                'symbol': symbol,
                'side': 'long',
                'price': current['close'],
                'stop_loss': round(stop_loss, 4),
                'take_profit': round(target, 4),
                'vwap': round(current['vwap'], 4),
                'lower_band': round(current['lower_band'], 4),
                'rsi_5m': round(current['rsi'], 2),
                'reason': f"L-band {current['lower_band']:.4f} RSI{current['rsi']:.1f} ADX{last_adx:.1f}",
                'strategy': 'S3_VWAP_Bands',
                'adx': round(last_adx, 2),
                'time': datetime.now(timezone.utc).isoformat(),
            }

    # ── Short setup: price at/above upper band AND RSI overbought ───────────
    if current['close'] >= current['upper_band'] and current['rsi'] > RSI_OVERBOUGHT:
        stop_loss = current['upper_band'] * 1.0015
        target = current['vwap']
        risk = stop_loss - current['close']
        if risk > 0:
            signal = {
                'symbol': symbol,
                'side': 'short',
                'price': current['close'],
                'stop_loss': round(stop_loss, 4),
                'take_profit': round(target, 4),
                'vwap': round(current['vwap'], 4),
                'upper_band': round(current['upper_band'], 4),
                'rsi_5m': round(current['rsi'], 2),
                'reason': f"U-band {current['upper_band']:.4f} RSI{current['rsi']:.1f} ADX{last_adx:.1f}",
                'strategy': 'S3_VWAP_Bands',
                'adx': round(last_adx, 2),
                'time': datetime.now(timezone.utc).isoformat(),
            }

    if signal:
        return signal, "✅ SIGNAL FOUND"
    return None, f"VWAP {current['vwap']:.4f}, bands [{current['lower_band']:.4f}-{current['upper_band']:.4f}], RSI {current['rsi']:.1f}"

# ── Main scanner ─────────────────────────────────────────────────────────────
def run_scanner(public_exchange):
    from utils.gate import check_liquidity
    print(f"\n{'='*65}")
    print(f"  STRATEGY 3 — VWAP Band Mean Reversion Scan")
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
        signal, msg = strategy3_signal(public_exchange, symbol)
        print(f"  Signal: {msg}")
        if signal:
            print(f"\n  🚨 TRADE SIGNAL 🚨")
            print(f"  Side:        {signal['side'].upper()}")
            print(f"  Entry:       ${signal['price']:,.4f}")
            print(f"  Stop Loss:   ${signal['stop_loss']:,.4f}")
            print(f"  Take Profit: ${signal['take_profit']:,.4f}")
            print(f"  VWAP:        ${signal.get('vwap', 'N/A'):,.4f}")
            print(f"  Band:        ${signal.get('lower_band' if signal['side']=='long' else 'upper_band', 0):,.4f}")
            print(f"  RSI (5m):    {signal.get('rsi_5m', 'N/A')}")
            print(f"  Reason:      {signal['reason']}")

if __name__ == "__main__":
    print("Connecting...")
    public, demo = connect()
    print("Connected. Scanning for Strategy 3 signals...")
    run_scanner(public)
