import ccxt
import os
from dotenv import load_dotenv
from datetime import datetime, timezone

load_dotenv()

# ---- CONFIG ----
COINS = [
    "BTC/USDT:USDT",
    "ETH/USDT:USDT",
    "SOL/USDT:USDT",
    "XRP/USDT:USDT",
    "BNB/USDT:USDT",
    "AVAX/USDT:USDT",
    "SUI/USDT:USDT",
    "DOGE/USDT:USDT",
]

LIQUIDITY_SPREAD_THRESHOLD = 0.0005    # 0.05%
LIQUIDITY_VOLUME_THRESHOLD = 50_000_000  # $50M


def connect_bybit():
    """
    Returns two exchange objects:
    - market_exchange: public, no API keys, reads live prices
    - demo_exchange:  testnet, authenticated, uses sandbox mode
    """
    # Public exchange – no keys needed, reads real-time prices
    market_exchange = ccxt.bybit({
        'options': {'defaultType': 'linear'},
    })

    # Demo exchange – testnet with sandbox mode
    demo_exchange = ccxt.bybit({
        'apiKey': os.getenv('BYBIT_API_KEY'),
        'secret': os.getenv('BYBIT_API_SECRET'),
        'uid': os.getenv('BYBIT_UID'),
        'options': {
            'defaultType': 'linear',
        },
    })
    demo_exchange.set_sandbox_mode(True)

    return market_exchange, demo_exchange


def liquidity_gate(exchange, symbol):
    try:
        # Spread check
        orderbook = exchange.fetch_order_book(symbol, limit=5)
        best_bid = orderbook['bids'][0][0]
        best_ask = orderbook['asks'][0][0]
        spread_pct = (best_ask - best_bid) / best_bid

        if spread_pct > LIQUIDITY_SPREAD_THRESHOLD:
            return False, f"Spread too wide: {spread_pct:.4%}"

        # Volume check - convert base volume to USD notional
        ohlcv = exchange.fetch_ohlcv(symbol, '1h', limit=2)
        last_candle = ohlcv[-2]                  # fully closed candle
        base_vol = last_candle[5]                # in base currency
        close_price = last_candle[4]
        volume_usd = base_vol * close_price      # USDT notional

        if volume_usd < LIQUIDITY_VOLUME_THRESHOLD:
            return False, f"Volume too low: ${volume_usd:,.0f}"

        return True, f"OK - spread {spread_pct:.4%}, vol ${volume_usd/1e6:.1f}M"

    except Exception as e:
        return False, f"Gate error: {str(e)}"

def scan_all_coins(market_exchange):
    print(f"\n{'='*70}")
    print(f"  MARKET SCAN — {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')} UTC")
    print(f"  Source: LIVE market data (real prices, real liquidity)")
    print(f"{'='*70}")

    results = {}
    for symbol in COINS:
        try:
            ticker = market_exchange.fetch_ticker(symbol)
            passed, reason = liquidity_gate(market_exchange, symbol)

            coin = symbol.split('/')[0]
            price = ticker['last']
            change = ticker['percentage']
            status = "✅ ACTIVE" if passed else "❌ SKIP"

            print(f"\n{coin:6} | ${price:>12,.4f} | {change:>+6.2f}% | {status}")
            print(f"       | {reason}")

            results[coin] = {
                'price': price,
                'change_pct': change,
                'liquidity_ok': passed,
                'reason': reason,
            }

        except Exception as e:
            print(f"\n{symbol} | ERROR: {e}")

    return results


if __name__ == "__main__":
    print("Connecting to Bybit...")
    market_exchange, demo_exchange = connect_bybit()
    print("Connected. Running market scan on REAL data...")

    # 1. Run the market scan
    scan_results = scan_all_coins(market_exchange)

    # 2. Check demo account access
    print(f"\n{'='*70}")
    print("  DEMO ACCOUNT STATUS")
    print(f"{'='*70}")
    try:
        balance = demo_exchange.fetch_balance()
        usdt = balance.get('USDT', {}).get('total', 0)
        print(f"  Demo USDT Balance: ${usdt:,.2f}")
        print("  ✅ Demo account connected successfully")
    except Exception as e:
        print(f"  ⚠️  Demo account error: {e}")
        if hasattr(e, 'response'):
            print(f"  Raw response: {e.response}")