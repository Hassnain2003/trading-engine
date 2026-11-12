import ccxt
import os
import pandas as pd
import pandas_ta as ta
from dotenv import load_dotenv
from datetime import datetime, timezone

load_dotenv()

# ── Constants ────────────────────────────────────────────────────────────────
COINS         = ["BTC/USDT:USDT", "XRP/USDT:USDT"]
SPREAD_LIMIT  = 0.0005   # 0.05%
VOLUME_MIN    = 50_000_000  # $50M per hour notional

RSI_1H_PERIOD   = 14
RSI_5M_PERIOD   = 7
RSI_OB          = 70     # overbought
RSI_OS          = 30     # oversold
ADX_PERIOD      = 14
ADX_MAX         = 25     # only trade in range/chop, not strong trend

ATR_PERIOD      = 14
ATR_SL_MULT     = 1.5   # stop = 1.5x ATR
ATR_TP_MULT     = 2.0   # target = 2x ATR

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

# ── Liquidity gate ────────────────────────────────────────────────────────────
def liquidity_gate(exchange, symbol):
    try:
        ob      = exchange.fetch_order_book(symbol, limit=5)
        bid     = ob['bids'][0][0]
        ask     = ob['asks'][0][0]
        spread  = (ask - bid) / bid

        ohlcv   = exchange.fetch_ohlcv(symbol, '1h', limit=2)
        candle  = ohlcv[-2]
        vol_usd = candle[5] * candle[4]   # base vol * close price = notional USD

        if spread > SPREAD_LIMIT:
            return False, f"Spread {spread:.4%} > limit"
        if vol_usd < VOLUME_MIN:
            return False, f"Vol ${vol_usd/1e6:.1f}M < $50M"
        return True, f"spread {spread:.4%} | vol ${vol_usd/1e6:.0f}M"

    except Exception as e:
        return False, f"Gate error: {e}"

# ── Fetch OHLCV and build DataFrame ──────────────────────────────────────────
def fetch_candles(exchange, symbol, timeframe, limit=100):
    raw = exchange.fetch_ohlcv(symbol, timeframe, limit=limit)
    df  = pd.DataFrame(raw, columns=['ts','open','high','low','close','volume'])
    df['ts'] = pd.to_datetime(df['ts'], unit='ms', utc=True)
    df.set_index('ts', inplace=True)
    return df

# ── Strategy 1 signal logic ───────────────────────────────────────────────────
def strategy1_signal(exchange, symbol):
    """
    Multi-Timeframe RSI Mean Reversion
    Context  (1h): RSI(14) must be <30 or >70
    Entry    (5m): RSI(7)  crosses back above 30 (long) / below 70 (short)
    Filter   (1h): ADX(14) < 25  — range environment only
    Returns  : dict with signal details, or None
    """

    # ── 1h context ───────────────────────────────────────────────────────────
    df_1h = fetch_candles(exchange, symbol, '1h', limit=50)

    rsi_1h = ta.rsi(df_1h['close'], length=RSI_1H_PERIOD)
    adx_1h = ta.adx(df_1h['high'], df_1h['low'], df_1h['close'], length=ADX_PERIOD)
    atr_1h = ta.atr(df_1h['high'], df_1h['low'], df_1h['close'], length=ATR_PERIOD)

    last_rsi_1h = rsi_1h.iloc[-1]
    last_adx    = adx_1h[f'ADX_{ADX_PERIOD}'].iloc[-1]
    last_atr    = atr_1h.iloc[-1]

    # Filter 1: ADX must show range (not trending)
    if last_adx >= ADX_MAX:
        return None, f"ADX {last_adx:.1f} >= {ADX_MAX} — trending, skip"

    # Filter 2: 1h RSI must be at extreme
    if last_rsi_1h >= RSI_OS and last_rsi_1h <= RSI_OB:
        return None, f"1h RSI {last_rsi_1h:.1f} — not at extreme, skip"

    context_direction = "long" if last_rsi_1h < RSI_OS else "short"

    # ── 5m entry ─────────────────────────────────────────────────────────────
    df_5m = fetch_candles(exchange, symbol, '5m', limit=20)
    rsi_5m = ta.rsi(df_5m['close'], length=RSI_5M_PERIOD)

    prev_rsi_5m = rsi_5m.iloc[-2]
    last_rsi_5m = rsi_5m.iloc[-1]
    current_price = df_5m['close'].iloc[-1]

    signal = None

    if context_direction == "long":
        # RSI(7) on 5m crosses back ABOVE 30
        if prev_rsi_5m < RSI_OS and last_rsi_5m >= RSI_OS:
            signal = {
                'symbol':    symbol,
                'side':      'long',
                'price':     current_price,
                'stop_loss': round(current_price - (ATR_SL_MULT * last_atr), 4),
                'take_profit': round(current_price + (ATR_TP_MULT * last_atr), 4),
                'rsi_1h':    round(last_rsi_1h, 2),
                'rsi_5m':    round(last_rsi_5m, 2),
                'adx':       round(last_adx, 2),
                'atr':       round(last_atr, 4),
                'reason':    f"1h RSI {last_rsi_1h:.1f} oversold, 5m RSI crossed above 30",
                'strategy':  'S1_RSI_Reversion',
                'time':      datetime.now(timezone.utc).isoformat(),
            }

    elif context_direction == "short":
        # RSI(7) on 5m crosses back BELOW 70
        if prev_rsi_5m > RSI_OB and last_rsi_5m <= RSI_OB:
            signal = {
                'symbol':    symbol,
                'side':      'short',
                'price':     current_price,
                'stop_loss': round(current_price + (ATR_SL_MULT * last_atr), 4),
                'take_profit': round(current_price - (ATR_TP_MULT * last_atr), 4),
                'rsi_1h':    round(last_rsi_1h, 2),
                'rsi_5m':    round(last_rsi_5m, 2),
                'adx':       round(last_adx, 2),
                'atr':       round(last_atr, 4),
                'reason':    f"1h RSI {last_rsi_1h:.1f} overbought, 5m RSI crossed below 70",
                'strategy':  'S1_RSI_Reversion',
                'time':      datetime.now(timezone.utc).isoformat(),
            }

    if signal:
        return signal, "✅ SIGNAL FOUND"
    return None, f"Context {context_direction} but no 5m crossover yet"

# ── Main scanner loop ─────────────────────────────────────────────────────────
def run_scanner(public_exchange):
    print(f"\n{'='*65}")
    print(f"  STRATEGY 1 — RSI Mean Reversion Scan")
    print(f"  {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')} UTC")
    print(f"{'='*65}")

    for symbol in COINS:
        coin = symbol.split('/')[0]
        print(f"\n── {coin} ──────────────────────────────────────")

        # Liquidity gate first
        gate_ok, gate_reason = liquidity_gate(public_exchange, symbol)
        if not gate_ok:
            print(f"  ❌ GATE FAIL: {gate_reason}")
            continue
        print(f"  ✅ Gate passed: {gate_reason}")

        # Strategy signal check
        signal, msg = strategy1_signal(public_exchange, symbol)
        print(f"  Signal: {msg}")

        if signal:
            print(f"\n  🚨 TRADE SIGNAL 🚨")
            print(f"  Side:        {signal['side'].upper()}")
            print(f"  Entry:       ${signal['price']:,.4f}")
            print(f"  Stop Loss:   ${signal['stop_loss']:,.4f}")
            print(f"  Take Profit: ${signal['take_profit']:,.4f}")
            print(f"  1h RSI:      {signal['rsi_1h']}")
            print(f"  5m RSI:      {signal['rsi_5m']}")
            print(f"  ADX:         {signal['adx']}")
            print(f"  Reason:      {signal['reason']}")

if __name__ == "__main__":
    print("Connecting...")
    public, demo = connect()
    print("Connected. Scanning for Strategy 1 signals...")
    run_scanner(public)
