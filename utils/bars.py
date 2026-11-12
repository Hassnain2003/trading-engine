"""Candle bar timing helpers for event-driven scans."""


def last_closed_bar_ts(exchange, symbol, timeframe):
    """Return UTC ms timestamp of the most recently *closed* candle."""
    ohlcv = exchange.fetch_ohlcv(symbol, timeframe, limit=3)
    if len(ohlcv) < 2:
        return None
    return int(ohlcv[-2][0])
