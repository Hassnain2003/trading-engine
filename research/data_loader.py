"""Data loader for the Stage 3 research harness.

Loads the locally downloaded Bybit public REST v5 dataset (data/*.csv, written
by data/download_data.py) into a single contiguous, de-duplicated, UTC-indexed
DataFrame per (symbol, timeframe).

The live strategies read candles through `strategies.strategyN.fetch_candles`,
which pulls a rolling window from the exchange. The harness reproduces that
exact window semantics from local data so a signal produced here is
bit-for-bit the signal the live engine would have produced on that bar.
"""

import glob
import os

import pandas as pd

DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'data')

# Symbol key used by the live engine (ccxt unified symbol) -> file prefix.
SYMBOL_FILE_KEY = {
    'BTC/USDT:USDT': 'BTCUSDT',
    'ETH/USDT:USDT': 'ETHUSDT',
}

TIMEFRAME_MS = {
    '5m': 300_000,
    '15m': 900_000,
    '1h': 3_600_000,
    '1d': 86_400_000,
}

_CACHE = {}


def _read_ohlcv_csv(symbol, timeframe):
    key = SYMBOL_FILE_KEY.get(symbol)
    if key is None:
        raise KeyError(f'no local data mapping for symbol {symbol!r}')
    pattern = os.path.join(DATA_DIR, f'{key}-USDT-USDT_{timeframe}_*.csv')
    files = sorted(glob.glob(pattern))
    if not files:
        raise FileNotFoundError(f'no data files for {symbol} {timeframe} (pattern {pattern})')
    use = ['ts_ms', 'open', 'high', 'low', 'close', 'volume']
    frames = []
    for path in files:
        try:
            frames.append(pd.read_csv(path, usecols=use))
        except ValueError:
            # Tolerate a chunk written without one of the optional columns.
            frames.append(pd.read_csv(path))
    df = pd.concat(frames, ignore_index=True)
    df = df[[c for c in use if c in df.columns]]
    df['ts'] = pd.to_datetime(df['ts_ms'], unit='ms', utc=True)
    df = (
        df.drop_duplicates(subset='ts')
          .set_index('ts')
          .sort_index()[['open', 'high', 'low', 'close', 'volume']]
    )
    return df


def load_ohlcv(symbol, timeframe):
    """Return the full contiguous OHLCV frame for (symbol, timeframe)."""
    ck = (symbol, timeframe)
    if ck not in _CACHE:
        _CACHE[ck] = _read_ohlcv_csv(symbol, timeframe)
    return _CACHE[ck]


def load_resampled(symbol, timeframe, base='5m'):
    """Resample the local base timeframe into `timeframe`.

    S2's trend filter uses 1h EMA ribbons but the download only stores
    5m/15m/1d. Bybit's own 1h candle is the UTC-hour aggregation of the 5m
    candles, so this reproduces it exactly (open=first, high=max, low=min,
    close=last, volume=sum) rather than approximating it.
    """
    ck = (symbol, f'resample:{timeframe}')
    if ck in _CACHE:
        return _CACHE[ck]
    base_df = load_ohlcv(symbol, base)
    rule = TIMEFRAME_MS[timeframe]
    out = base_df.resample(f'{rule}ms', origin='epoch', label='left',
                           closed='left').agg(
        {'open': 'first', 'high': 'max', 'low': 'min',
         'close': 'last', 'volume': 'sum'})
    # Only keep bars that have data — resample() creates empty bars for
    # periods with no trades, which would poison ATR/EMA.
    out = out.dropna(subset=['open', 'high', 'low', 'close'])
    _CACHE[ck] = out
    return out


def load_funding(symbol):
    """Return the funding-rate frame (indexed by funding timestamp, UTC)."""
    ck = (symbol, 'funding')
    if ck in _CACHE:
        return _CACHE[ck]
    key = SYMBOL_FILE_KEY.get(symbol)
    path = os.path.join(DATA_DIR, f'{key}-USDT-USDT_funding_20231020_20261019.csv')
    df = pd.read_csv(path)
    df['ts'] = pd.to_datetime(df['funding_rate_timestamp_ms'], unit='ms', utc=True)
    df = df.drop_duplicates(subset='ts').set_index('ts').sort_index()
    df = df[['funding_rate']]
    _CACHE[ck] = df
    return df


def window(df, end_ts, n):
    """The last `n` rows of `df` up to and including `end_ts`.

    Mirrors `fetch_candles(exchange, symbol, tf, limit=n)`: the live code asks
    the venue for the most recent n candles and pandas-TA then computes the
    indicator over exactly that window, so the EMA seed differs from a
    full-history EMA. Reproducing the window (not the full series) is what
    makes the harness match live numbers.
    """
    end_pos = df.index.searchsorted(end_ts, side='right')
    if end_pos <= 0:
        return df.iloc[0:0]
    return df.iloc[max(0, end_pos - n):end_pos]
