import ccxt

SPREAD_LIMIT = 0.0005

# Minimum hourly notional volume (USD) — tiered by liquidity
VOLUME_MIN_MAJOR = 2_000_000   # BTC, ETH (demo)
VOLUME_MIN_ALT = 500_000       # all other alts (demo)

VOLUME_TIERS = {
    'BTC/USDT:USDT': VOLUME_MIN_MAJOR,
    'ETH/USDT:USDT': VOLUME_MIN_MAJOR,
}

# Legacy alias
VOLUME_MIN = VOLUME_MIN_MAJOR


def volume_min_for(symbol):
    return VOLUME_TIERS.get(symbol, VOLUME_MIN_ALT)


def _hourly_notional_usd(exchange, symbol):
    """Last completed 1h candle quote volume (volume × close)."""
    ohlcv = exchange.fetch_ohlcv(symbol, '1h', limit=2)
    candle = ohlcv[-2]
    return float(candle[5]) * float(candle[4])


def check_liquidity(exchange, symbol):
    """Spread from order book; volume from last completed 1h candle."""
    try:
        ob = exchange.fetch_order_book(symbol, limit=5)
        bid = ob['bids'][0][0]
        ask = ob['asks'][0][0]
        spread = (ask - bid) / bid

        vol_usd = _hourly_notional_usd(exchange, symbol)
        vol_min = volume_min_for(symbol)

        if spread > SPREAD_LIMIT:
            return False, f"Spread {spread:.4%} > limit"
        if vol_usd < vol_min:
            return False, f"Hourly vol ${vol_usd/1e6:.1f}M < ${vol_min/1e6:.0f}M"
        return True, f"spread {spread:.4%} | hourly vol ${vol_usd/1e6:.0f}M"
    except Exception as e:
        return False, f"Gate error: {e}"
