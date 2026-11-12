"""Market data collection, correlation matrix, and trade counting."""

import logging
import time
from datetime import datetime, timezone

from execution.config import CORRELATION_REFRESH_SEC, MARKET_DATA_REFRESH_SEC
from utils.correlation import build_correlation_matrix

logger = logging.getLogger('agent_execution')


def collect_market_data(public, strategies, agent_state, market_data_refresh_sec=MARKET_DATA_REFRESH_SEC):
    """Fetch ticker + order book for all strategy symbols, cached by refresh interval."""
    now = time.time()
    cached = agent_state.get('market_data') or {}
    if cached and now - agent_state.get('_market_data_at', 0) < market_data_refresh_sec:
        return cached

    all_coins = set()
    for _, _, coins in strategies:
        all_coins.update(coins)
    data = {}
    for symbol in sorted(all_coins):
        try:
            ticker = public.fetch_ticker(symbol)
            ob = public.fetch_order_book(symbol, limit=5)
            bid = ob['bids'][0][0]
            ask = ob['asks'][0][0]
            spread = (ask - bid) / bid
            quote_vol = float(ticker.get('quoteVolume') or 0)
            spread_msg = f"spread {spread:.4%} | 24h vol ${quote_vol / 1e6:.0f}M"
            data[symbol] = {
                'price': ticker['last'],
                'change': ticker['percentage'],
                'volume': quote_vol,
                'spread': spread_msg,
                'funding_rate': ticker.get('info', {}).get('fundingRate', 'N/A'),
                'volatility_vs_30d': 'N/A',
                'btc_dominance_shift': 'N/A',
            }
        except Exception as e:
            logger.warning(f"Market data failed for {symbol}: {e}")
            if symbol in cached:
                data[symbol] = cached[symbol]
        time.sleep(0.1)
    agent_state['_market_data_at'] = now
    return data


def refresh_correlation_matrix(public, strategies, agent_state,
                                correlation_refresh_sec=CORRELATION_REFRESH_SEC):
    """Build and cache the correlation matrix across strategy symbols."""
    now = time.time()
    cached = agent_state.get('correlation_matrix')
    if cached is not None and now - agent_state.get('_correlation_at', 0) < correlation_refresh_sec:
        return cached
    matrix = build_correlation_matrix(
        public,
        symbols=sorted({s for _, _, coins in strategies for s in coins}),
    )
    agent_state['_correlation_at'] = now
    return matrix


def count_trades_today(conn):
    c = conn.cursor()
    today = datetime.now(timezone.utc).strftime('%Y-%m-%d')
    c.execute(
        "SELECT COUNT(*) FROM signals WHERE timestamp LIKE ? AND status IN ('open','closed')",
        (f"{today}%",),
    )
    return c.fetchone()[0]


def ticker_price(public, symbol):
    try:
        return float(public.fetch_ticker(symbol)['last'])
    except Exception:
        return None


def candles_fresh(public, symbol, timeframe, max_age_sec=600):
    """Check whether the latest closed candle is fresh enough to trade on.

    Returns (fresh: bool, age_sec: float | None).
    Fails open (fresh=True, age=None) on fetch errors so a transient API
    blip doesn't block entries — the ApiHealth circuit breaker handles that.
    """
    from utils.bars import last_closed_bar_ts
    try:
        bar_ts = last_closed_bar_ts(public, symbol, timeframe)
    except Exception as e:
        logger.warning(f"Staleness check failed for {symbol} {timeframe}: {e}")
        return True, None
    if bar_ts is None:
        return False, None
    age = time.time() - bar_ts / 1000
    return age <= max_age_sec, age
