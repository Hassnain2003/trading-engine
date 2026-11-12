"""A local, replayable stand-in for the ccxt exchange used by the live
strategy modules.

The live strategies (S2/S7/S9) are plain functions of (exchange, symbol). They
call `exchange.fetch_ohlcv(symbol, timeframe, limit=..., since=...)` and
`exchange.parse8601(...)`, and they read the wall clock via
`datetime.now(timezone.utc)`.

To replay them honestly over 3 years of downloaded candles we need:

  * a data source that serves exactly the candles that existed at the
    simulated wall-clock time (no lookahead),
  * the same "last bar is the in-progress bar" convention the real venue
    uses (verified against the live API: `fetch_ohlcv` returns the currently
    forming bar as its last element), and
  * a clock the strategies can be pinned to.

`ReplayExchange` provides the data. `patched_now` (below) provides the clock.

FORMING-BAR SYNTHESIS (lookahead fix, walk-forward harness)
----------------------------------------------------------
With `synth_forming=True` (the walk-forward harness sets this; the live-API
validator leaves it False) the still-forming bar of ANY timeframe is rebuilt
from the base 5m candles that had completed at the pinned instant — never
from the dataset's finalized values, which would leak the future:

  * forming 5m bar at pinned T: no intra-bar data exists yet → flat at the
    previous bar's close, volume 0 (what the venue serves seconds into a bar);
  * forming 1h/1d bar at pinned T: OHLCV aggregated from the hour's/day's
    completed 5m bars only — exactly what Bybit serves mid-bar.

With `synth_forming=False` (default) the finalized dataset row is served for
the forming bar, matching the pre-existing behaviour that
`research/validate_replay.py` was written against (it excludes the forming
bar from equality checks anyway).
"""

import time
from datetime import datetime, timezone

import pandas as pd

from research.data_loader import TIMEFRAME_MS, load_ohlcv, load_resampled

_MARKET_META_CACHE = None  # frozen Bybit instrument snapshot, loaded lazily


class ReplayExchange:
    """Serves candles from the local dataset as of a simulated `now`.

    Parameters
    ----------
    now : datetime
        The simulated wall clock (UTC).
    symbols : list[str]
        Unified ccxt symbols to serve, e.g. ['BTC/USDT:USDT'].
    synth_forming : bool
        Rebuild the forming bar of every timeframe from completed base 5m
        candles instead of serving the finalized dataset row (no lookahead).
    """

    def __init__(self, now, symbols, synth_forming=False):
        self.now = now
        self.symbols = list(symbols)
        self.synth_forming = synth_forming

    def set_now(self, now):
        """Advance the pinned wall clock (walk-forward loop)."""
        self.now = now

    # ── ccxt API surface used by the strategies ───────────────────────────
    def parse8601(self, s):
        return int(pd.Timestamp(s).timestamp() * 1000)

    def milliseconds(self):
        return int(self.now.timestamp() * 1000)

    def fetch_ohlcv(self, symbol, timeframe='5m', limit=100, since=None, params=None):
        """Reproduce ccxt's Bybit fetch_ohlcv semantics exactly.

        Verified against the live API on 2026-10-19:
          * WITHOUT `since`: the LAST `limit` bars, the last one being the
            still-forming bar (Bybit serves a bar whose interval has not
            finished yet — e.g. a 21:35 bar served at 21:36 wall clock).
          * WITH `since`: the FIRST `limit` bars at or after `since`,
            oldest-first. (Not the most recent ones — this is easy to get
            wrong and would silently change every intraday strategy.)
          * `since` is epoch MILLISECONDS and the bar exactly at `since`
            IS included.
        """
        df = self._frame(symbol, timeframe)
        if df.empty:
            return []

        idx = df.index
        tf = pd.Timedelta(milliseconds=TIMEFRAME_MS[timeframe])
        # Bars with ts <= now → complete bars + (at most) one forming bar.
        end = int(idx.searchsorted(self.now, side='right'))
        # Bars with ts + tf <= now → complete bars only.
        complete_end = int(idx.searchsorted(self.now - tf, side='right'))

        start = 0
        if since is not None:
            start = int(idx.searchsorted(
                pd.Timestamp(int(since), unit='ms', tz='UTC'), side='left'))
        if start >= end:
            return []

        n_vis = end - start
        if limit is not None and n_vis > limit:
            # With `since` the venue keeps the OLDEST bars; without it, the
            # most recent ones.
            if since is not None:
                n_vis = limit
            else:
                start = end - limit
                n_vis = limit

        part = df.iloc[start:start + n_vis]
        vals = part[['open', 'high', 'low', 'close', 'volume']].to_numpy(dtype=float)
        # pandas 3: asi8 is expressed in the dtype unit (.unit: 's'/'ms'/...),
        # while .resolution is only a granularity hint ('minute'/'day'/...).
        _TO_MS = {'s': 1_000, 'ms': 1, 'us': 0.001, 'ns': 1e-6}
        tss = (part.index.asi8 * _TO_MS[part.index.unit]).astype('int64')
        n_complete = max(0, min(n_vis, complete_end - start))

        if self.synth_forming and n_vis > n_complete:
            vals = vals.copy()
            vals[n_complete:] = self._synth_forming(
                symbol, timeframe, part.index[n_complete])

        # ccxt returns [[ts_ms, open, high, low, close, volume], ...]
        return [[int(tss[i]), float(vals[i][0]), float(vals[i][1]),
                 float(vals[i][2]), float(vals[i][3]), float(vals[i][4])]
                for i in range(n_vis)]

    def _synth_forming(self, symbol, timeframe, bar_ts):
        """(o, h, l, c, v) for the forming bar starting at `bar_ts`.

        Built only from base-5m candles that were COMPLETE at `self.now`.
        If no base candle completed inside the bar yet, the bar is flat at
        the previous close (venue behaviour seconds into a new bar).
        """
        base = load_ohlcv(symbol, '5m')
        cutoff = self.now - pd.Timedelta(minutes=5)
        lo = int(base.index.searchsorted(bar_ts, side='left'))
        hi = int(base.index.searchsorted(cutoff, side='right'))
        if hi > lo:
            sub = base.iloc[lo:hi]
            return (float(sub['open'].iloc[0]), float(sub['high'].max()),
                    float(sub['low'].min()), float(sub['close'].iloc[-1]),
                    float(sub['volume'].sum()))
        frame = self._frame(symbol, timeframe)
        cut = int(frame.index.searchsorted(bar_ts, side='left'))
        if cut == 0:
            last_close = float(base['close'].iloc[-1])
        else:
            last_close = float(frame['close'].iloc[cut - 1])
        return (last_close, last_close, last_close, last_close, 0.0)

    @staticmethod
    def bars_as_of(df, timeframe, now):
        """Return the bars a venue would serve at wall-clock `now`.

        A bar stamped `t` covers the interval [t, t + timeframe). It is
        COMPLETE once `now >= t + timeframe`, and still FORMING while
        `now` is inside the interval. The venue serves the forming bar as
        its last element, so it is included here exactly once.
        """
        tf = pd.Timedelta(milliseconds=TIMEFRAME_MS[timeframe])
        complete = df[df.index + tf <= now]
        forming = df[(df.index <= now) & (df.index + tf > now)]
        if len(forming):
            return pd.concat([complete, forming.iloc[:1]])
        return complete

    @staticmethod
    def _interval(timeframe):
        return pd.Timedelta(milliseconds=TIMEFRAME_MS[timeframe])

    # ── venue surface for execution-side helpers ─────────────────────────
    # ensure_min_exchange_qty() (execution/entry_scanner.py) needs
    # demo.market(symbol)['limits']['amount']['min'] and amount_to_precision;
    # both are backed by the frozen Bybit instrument snapshot in
    # research/market_meta.json so the harness cannot drift from the venue.
    # check_liquidity() (utils/gate.py) needs fetch_order_book + fetch_ohlcv.
    @staticmethod
    def _meta(symbol):
        import json
        import os
        global _MARKET_META_CACHE
        if _MARKET_META_CACHE is None:
            path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                'market_meta.json')
            with open(path) as f:
                _MARKET_META_CACHE = json.load(f)
        return _MARKET_META_CACHE[symbol]

    def market(self, symbol):
        m = self._meta(symbol)
        return {
            'id': m['id'], 'symbol': symbol,
            'precision': {'amount': m['qty_step'], 'price': m['tick_size']},
            'limits': {
                'amount': {'min': m['min_qty'], 'max': m['max_qty']},
                'cost': {'min': m['min_notional']},
            },
        }

    def amount_to_precision(self, symbol, qty):
        import math
        step = float(self._meta(symbol)['qty_step'])
        return math.floor(qty / step + 1e-9) * step

    def price_to_precision(self, symbol, price):
        tick = float(self._meta(symbol)['tick_size'])
        return round(round(price / tick) * tick, 10)

    def fetch_order_book(self, symbol, limit=5, params=None):
        """Synthetic zero-spread book at the last price (documented deviation
        D5: the spread test can never fail in replay; the 1h-notional test is
        the binding one and is reproduced faithfully)."""
        rows = self.fetch_ohlcv(symbol, '5m', limit=1)
        if not rows:
            return {'bids': [], 'asks': []}
        px = float(rows[-1][4])
        return {'bids': [[px, 1000.0]], 'asks': [[px, 1000.0]]}

    def fetch_ticker(self, symbol, params=None):
        rows = self.fetch_ohlcv(symbol, '5m', limit=1)
        px = float(rows[-1][4]) if rows else 0.0
        return {'symbol': symbol, 'last': px, 'bid': px, 'ask': px}

    # ── internals ────────────────────────────────────────────────────────
    def _frame(self, symbol, timeframe):
        if timeframe == '1h':
            return load_resampled(symbol, '1h')
        return load_ohlcv(symbol, timeframe)

class _FrozenDatetime(datetime):
    """datetime subclass whose now() is pinned to a fixed instant."""
    _frozen = None

    @classmethod
    def now(cls, tz=None):
        if cls._frozen is None:
            return datetime.now(tz)
        if tz is None:
            return cls._frozen.replace(tzinfo=None)
        return cls._frozen.astimezone(tz)

    @classmethod
    def utcnow(cls):
        return cls.now(timezone.utc)


def patched_now(dt):
    """Context manager pinning datetime.now()/utcnow() to `dt`.

    The strategy modules do `from datetime import datetime, timezone`, so the
    name `datetime` is bound inside their own module namespace. We patch each
    module's `datetime` attribute for the duration of the call.
    """
    class _Ctx:
        def __init__(self, modules, dt):
            self.modules = list(modules)
            self.dt = dt
            self._saved = []

        def __enter__(self):
            frozen = _FrozenDatetime
            frozen._frozen = self.dt
            for mod in self.modules:
                self._saved.append((mod, mod.datetime))
                mod.datetime = frozen
            return self

        def __exit__(self, *exc):
            for mod, original in self._saved:
                mod.datetime = original
            _FrozenDatetime._frozen = None
            return False

    return _Ctx


def strategy_modules():
    """Import (once) the live strategy modules whose clocks we patch."""
    from strategies import strategy2_breakout_retest as s2
    from strategies import strategy7_daily_momentum as s7
    return [s2, s7]
