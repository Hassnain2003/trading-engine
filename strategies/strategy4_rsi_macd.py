import ccxt
import os
import pandas as pd
import pandas_ta as ta
from dotenv import load_dotenv
from datetime import datetime, timezone

load_dotenv()

COINS         = ["BTC/USDT:USDT", "ETH/USDT:USDT", "LINK/USDT:USDT", "DOGE/USDT:USDT"]

RSI_PERIOD    = 14
MACD_FAST     = 12
MACD_SLOW     = 26
MACD_SIGNAL   = 9
RSI_OS        = 35          # DEMO: oversold threshold
RSI_OB        = 65          # DEMO: overbought threshold

# ── Exchange ─────────────────────────────────────────────────────────────────
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

def fetch_candles(exchange, symbol, timeframe, limit=100):
    raw = exchange.fetch_ohlcv(symbol, timeframe, limit=limit)
    df = pd.DataFrame(raw, columns=['ts','open','high','low','close','volume'])
    df['ts'] = pd.to_datetime(df['ts'], unit='ms', utc=True)
    df.set_index('ts', inplace=True)
    return df

def strategy4_signal(exchange, symbol):
    """
    RSI Divergence + MACD – DEMO RELAXED
    Enter on either:
    1) Classic divergence + MACD confirmation (original logic)
    2) RSI extreme (oversold/overbought) + MACD bullish/bearish crossover
    No volume spike required for demo testing.
    """
    df = fetch_candles(exchange, symbol, '15m', limit=60)
    if len(df) < 50:
        return None, "Not enough data"

    rsi = ta.rsi(df['close'], length=RSI_PERIOD)
    macd = ta.macd(df['close'], fast=MACD_FAST, slow=MACD_SLOW, signal=MACD_SIGNAL)
    macd_hist = macd['MACDh_12_26_9']
    macd_line = macd['MACD_12_26_9']
    signal_line = macd['MACDs_12_26_9']

    last_rsi = rsi.iloc[-1]
    current_price = df['close'].iloc[-1]
    signal = None

    # Check for divergence (original logic, but no volume requirement)
    window = df.iloc[-25:]
    lows = window['close'][(window['close'] < window['close'].shift(1)) & (window['close'] < window['close'].shift(-1))]
    highs = window['close'][(window['close'] > window['close'].shift(1)) & (window['close'] > window['close'].shift(-1))]

    # ── Bullish Divergence ────────────────────────────────────────────────
    if len(lows) >= 2:
        price_low1 = lows.iloc[-2]
        price_low2 = lows.iloc[-1]
        idx1, idx2 = lows.index[-2], lows.index[-1]
        rsi_low1, rsi_low2 = rsi.loc[idx1], rsi.loc[idx2]
        if price_low2 < price_low1 and rsi_low2 > rsi_low1:
            if (macd_hist.iloc[-1] > macd_hist.iloc[-2]) or (macd_line.iloc[-1] > signal_line.iloc[-1]):
                entry = current_price
                stop = min(df['low'].iloc[-10:])
                target = entry + 2 * (entry - stop)
                signal = {
                    'symbol': symbol, 'side': 'long', 'price': entry,
                    'stop_loss': round(stop, 4), 'take_profit': round(target, 4),
                    'reason': f"Bull div: RSI HL, MACD confirmed",
                    'strategy': 'S4_RSI_MACD',
                    'order_type': 'ioc',   # DEMO: immediate fill
                    'time': datetime.now(timezone.utc).isoformat(),
                }

    # ── Bearish Divergence ─────────────────────────────────────────────────
    if len(highs) >= 2:
        price_high1 = highs.iloc[-2]
        price_high2 = highs.iloc[-1]
        idx1, idx2 = highs.index[-2], highs.index[-1]
        rsi_high1, rsi_high2 = rsi.loc[idx1], rsi.loc[idx2]
        if price_high2 > price_high1 and rsi_high2 < rsi_high1:
            if (macd_hist.iloc[-1] < macd_hist.iloc[-2]) or (macd_line.iloc[-1] < signal_line.iloc[-1]):
                entry = current_price
                stop = max(df['high'].iloc[-10:])
                target = entry - 2 * (stop - entry)
                signal = {
                    'symbol': symbol, 'side': 'short', 'price': entry,
                    'stop_loss': round(stop, 4), 'take_profit': round(target, 4),
                    'reason': f"Bear div: RSI LH, MACD confirmed",
                    'strategy': 'S4_RSI_MACD',
                    'order_type': 'ioc',   # DEMO: immediate fill
                    'time': datetime.now(timezone.utc).isoformat(),
                }

    # ── Fallback: RSI extreme + MACD crossover (no divergence needed) ──────
    if signal is None and last_rsi < RSI_OS:
        if macd_line.iloc[-1] > signal_line.iloc[-1] and macd_line.iloc[-2] <= signal_line.iloc[-2]:
            entry = current_price
            stop = min(df['low'].iloc[-10:])
            target = entry + 2 * (entry - stop)
            signal = {
                'symbol': symbol, 'side': 'long', 'price': entry,
                'stop_loss': round(stop, 4), 'take_profit': round(target, 4),
                'reason': f"RSI OS {last_rsi:.1f} + MACD bull cross",
                'strategy': 'S4_RSI_MACD',
                'order_type': 'ioc',   # DEMO: immediate fill
                'time': datetime.now(timezone.utc).isoformat(),
            }

    if signal is None and last_rsi > RSI_OB:
        if macd_line.iloc[-1] < signal_line.iloc[-1] and macd_line.iloc[-2] >= signal_line.iloc[-2]:
            entry = current_price
            stop = max(df['high'].iloc[-10:])
            target = entry - 2 * (stop - entry)
            signal = {
                'symbol': symbol, 'side': 'short', 'price': entry,
                'stop_loss': round(stop, 4), 'take_profit': round(target, 4),
                'reason': f"RSI OB {last_rsi:.1f} + MACD bear cross",
                'strategy': 'S4_RSI_MACD',
                'order_type': 'ioc',   # DEMO: immediate fill
                'time': datetime.now(timezone.utc).isoformat(),
            }

    if signal:
        return signal, "✅ SIGNAL FOUND"
    return None, f"No divergence confirmed (last RSI {last_rsi:.1f})"

def run_scanner(public_exchange):
    from utils.gate import check_liquidity
    print(f"\n{'='*65}")
    print(f"  STRATEGY 4 — RSI Divergence + MACD Scan")
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
        signal, msg = strategy4_signal(public_exchange, symbol)
        print(f"  Signal: {msg}")
        if signal:
            print(f"\n  🚨 TRADE SIGNAL 🚨")
            print(f"  Side:        {signal['side'].upper()}")
            print(f"  Entry:       ${signal['price']:,.4f}")
            print(f"  Stop Loss:   ${signal['stop_loss']:,.4f}")
            print(f"  Take Profit: ${signal['take_profit']:,.4f}")
            print(f"  Reason:      {signal['reason']}")

if __name__ == "__main__":
    print("Connecting...")
    public, demo = connect()
    print("Connected. Scanning...")
    run_scanner(public)
