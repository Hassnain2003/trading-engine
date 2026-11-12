import ccxt
import os
import pandas as pd
import pandas_ta as ta
from dotenv import load_dotenv
from datetime import datetime, timezone

load_dotenv()

COINS         = ["DOGE/USDT:USDT", "BNB/USDT:USDT", "SUI/USDT:USDT"]
SPREAD_LIMIT  = 0.0005
VOLUME_MIN    = 50_000_000

EMA_FAST      = 9
EMA_SLOW      = 21
EMA_TREND     = 50       # 1h trend filter
ATR_PERIOD    = 14
ATR_SL_MULT   = 2.0
ATR_TP_MULT   = 3.0

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

def liquidity_gate(exchange, symbol):
    try:
        ob = exchange.fetch_order_book(symbol, limit=5)
        bid, ask = ob['bids'][0][0], ob['asks'][0][0]
        spread = (ask - bid) / bid
        ohlcv = exchange.fetch_ohlcv(symbol, '1h', limit=2)
        candle = ohlcv[-2]
        vol_usd = candle[5] * candle[4]
        if spread > SPREAD_LIMIT:
            return False, f"Spread {spread:.4%}"
        if vol_usd < VOLUME_MIN:
            return False, f"Vol ${vol_usd/1e6:.1f}M < $50M"
        return True, f"spread {spread:.4%} | vol ${vol_usd/1e6:.0f}M"
    except Exception as e:
        return False, f"Gate error: {e}"

def fetch_candles(exchange, symbol, timeframe, limit=100):
    raw = exchange.fetch_ohlcv(symbol, timeframe, limit=limit)
    df = pd.DataFrame(raw, columns=['ts','open','high','low','close','volume'])
    df['ts'] = pd.to_datetime(df['ts'], unit='ms', utc=True)
    df.set_index('ts', inplace=True)
    return df

def strategy5_signal(exchange, symbol):
    """
    MA Crossover Trend Following
    - 1h 50 EMA sets directional bias (price above = longs only, below = shorts only).
    - 5m chart: 9 EMA crosses above 21 EMA for long, below for short.
    - Crossover candle volume must exceed 5-period average.
    """
    # 1h trend context
    df_1h = fetch_candles(exchange, symbol, '1h', limit=50)
    ema_trend = ta.ema(df_1h['close'], length=EMA_TREND)
    last_ema_trend = ema_trend.iloc[-1]
    current_price_1h = df_1h['close'].iloc[-1]
    trend_bias = "long" if current_price_1h > last_ema_trend else "short"

    # ATR for stops
    atr_1h = ta.atr(df_1h['high'], df_1h['low'], df_1h['close'], length=ATR_PERIOD)
    last_atr = atr_1h.iloc[-1]

    # 5m crossover
    df_5m = fetch_candles(exchange, symbol, '5m', limit=50)
    ema_fast = ta.ema(df_5m['close'], length=EMA_FAST)
    ema_slow = ta.ema(df_5m['close'], length=EMA_SLOW)

    prev_fast = ema_fast.iloc[-2]
    prev_slow = ema_slow.iloc[-2]
    curr_fast = ema_fast.iloc[-1]
    curr_slow = ema_slow.iloc[-1]

    current_vol = df_5m['volume'].iloc[-1]
    avg_vol_5 = df_5m['volume'].iloc[-6:-1].mean()

    current_price = df_5m['close'].iloc[-1]
    signal = None

    # ── Long ─────────────────────────────────────────────────────────────────
    if trend_bias == "long":
        if prev_fast <= prev_slow and curr_fast > curr_slow and current_vol > avg_vol_5:
            stop = current_price - (ATR_SL_MULT * last_atr)
            target = current_price + (ATR_TP_MULT * last_atr)
            signal = {
                'symbol': symbol,
                'side': 'long',
                'price': current_price,
                'stop_loss': round(stop, 4),
                'take_profit': round(target, 4),
                'ema_trend': round(last_ema_trend, 2),
                'reason': f"Bullish cross on 5m, price above 1h 50 EMA {last_ema_trend:.2f}, vol spike {current_vol:.0f} > {avg_vol_5:.0f}",
                'strategy': 'S5_MA_Crossover',
                'time': datetime.now(timezone.utc).isoformat(),
            }

    # ── Short ────────────────────────────────────────────────────────────────
    if trend_bias == "short":
        if prev_fast >= prev_slow and curr_fast < curr_slow and current_vol > avg_vol_5:
            stop = current_price + (ATR_SL_MULT * last_atr)
            target = current_price - (ATR_TP_MULT * last_atr)
            signal = {
                'symbol': symbol,
                'side': 'short',
                'price': current_price,
                'stop_loss': round(stop, 4),
                'take_profit': round(target, 4),
                'ema_trend': round(last_ema_trend, 2),
                'reason': f"Bearish cross on 5m, price below 1h 50 EMA {last_ema_trend:.2f}, vol spike {current_vol:.0f} > {avg_vol_5:.0f}",
                'strategy': 'S5_MA_Crossover',
                'time': datetime.now(timezone.utc).isoformat(),
            }

    if signal:
        return signal, "✅ SIGNAL FOUND"
    return None, f"No crossover. 1h trend: {trend_bias} (EMA50={last_ema_trend:.2f})"

def run_scanner(public_exchange):
    print(f"\n{'='*65}")
    print(f"  STRATEGY 5 — MA Crossover Trend Following")
    print(f"  {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')} UTC")
    print(f"{'='*65}")

    for symbol in COINS:
        coin = symbol.split('/')[0]
        print(f"\n── {coin} ──────────────────────────────────────")
        gate_ok, gate_reason = liquidity_gate(public_exchange, symbol)
        if not gate_ok:
            print(f"  ❌ GATE FAIL: {gate_reason}")
            continue
        print(f"  ✅ Gate passed: {gate_reason}")
        signal, msg = strategy5_signal(public_exchange, symbol)
        print(f"  Signal: {msg}")
        if signal:
            print(f"\n  🚨 TRADE SIGNAL 🚨")
            print(f"  Side:        {signal['side'].upper()}")
            print(f"  Entry:       ${signal['price']:,.4f}")
            print(f"  Stop Loss:   ${signal['stop_loss']:,.4f}")
            print(f"  Take Profit: ${signal['take_profit']:,.4f}")
            print(f"  1h 50 EMA:   ${signal['ema_trend']:,.2f}")
            print(f"  Reason:      {signal['reason']}")

if __name__ == "__main__":
    print("Connecting...")
    public, demo = connect()
    print("Connected. Scanning...")
    run_scanner(public)
