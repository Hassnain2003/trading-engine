"""Rolling return correlation for active trading symbols."""

import logging
import time

import pandas as pd

logger = logging.getLogger('correlation')

CORRELATION_SYMBOLS = [
    'BTC/USDT:USDT',
    'ETH/USDT:USDT',
    'SOL/USDT:USDT',
    'BNB/USDT:USDT',
    'XRP/USDT:USDT',
    'LINK/USDT:USDT',
    'DOGE/USDT:USDT',
]


def build_correlation_matrix(public_exchange, symbols=None, timeframe='1h', limit=168):
    """Return DataFrame of pairwise return correlations (default ~7 days of 1h bars)."""
    symbols = symbols or CORRELATION_SYMBOLS
    returns = {}

    for symbol in symbols:
        try:
            raw = public_exchange.fetch_ohlcv(symbol, timeframe, limit=limit)
            if len(raw) < 20:
                continue
            df = pd.DataFrame(raw, columns=['ts', 'open', 'high', 'low', 'close', 'volume'])
            label = symbol.split('/')[0]
            returns[label] = df['close'].pct_change().dropna()
        except Exception as e:
            logger.warning(f"Correlation data failed for {symbol}: {e}")
        time.sleep(0.15)

    if len(returns) < 2:
        return pd.DataFrame()

    combined = pd.DataFrame(returns).dropna()
    if combined.empty:
        return pd.DataFrame()
    return combined.corr()
