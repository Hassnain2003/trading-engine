"""Validate ReplayExchange against the live Bybit API.

Two independent checks:

  1. HISTORICAL INSTANTS. For a simulated wall-clock instant T in the past,
     the venue still serves the *finalized* bars around T. We fetch that
     window live and compare it bar-for-bar with the local dataset. Finalized
     history must match exactly — if it does not, the download or the
     resampling is wrong.

  2. THE CURRENT INSTANT. We compare the live API against the replay at the
     real wall clock. Every *complete* bar must match exactly. The final bar
     is the still-forming one: the venue reports its live OHLCV, whereas the
     local dataset holds the finalized version, so that single bar is
     expected to differ in close/volume and is excluded from the equality
     check (its timestamp and open must still match).

Run:  venv/bin/python -m research.validate_replay
"""

import sys
from datetime import datetime, timedelta, timezone

import ccxt
import pandas as pd

from research.data_loader import TIMEFRAME_MS
from research.replay_exchange import ReplayExchange

SYMBOLS = ['BTC/USDT:USDT', 'ETH/USDT:USDT']
FIELDS = ['open', 'high', 'low', 'close', 'volume']

# Set by main(); the newest timestamp the local dataset can serve.
cutoff = None


def dataset_cutoff():
    """The newest timestamp the local dataset can serve.

    Read from data/cutoff.txt, which the downloader wrote when it ran.
    """
    import os
    from pathlib import Path
    path = Path(__file__).resolve().parent.parent / 'data' / 'cutoff.txt'
    if not path.exists():
        raise RuntimeError(f'data/cutoff.txt not found at {path}')
    text = path.read_text()
    for line in text.splitlines():
        if line.startswith('cutoff_utc='):
            return pd.Timestamp(line.split('=', 1)[1])
    raise RuntimeError('cutoff_utc not found in data/cutoff.txt')


def _rows(raw):
    """Normalise ccxt output to {ts: (o,h,l,c,v)}."""
    return {
        pd.Timestamp(r[0], unit='ms', tz='UTC'): tuple(float(x) for x in r[1:6])
        for r in raw
    }


def _diff(live, mine):
    """Return a list of human-readable mismatches between two bar dicts."""
    problems = []
    only_live = sorted(set(live) - set(mine))
    only_mine = sorted(set(mine) - set(live))
    if only_live:
        problems.append(f'{len(only_live)} bars only in live, e.g. {only_live[:2]}')
    if only_mine:
        problems.append(f'{len(only_mine)} bars only in replay, e.g. {only_mine[:2]}')
    for ts in sorted(set(live) & set(mine)):
        a, b = live[ts], mine[ts]
        for i, name in enumerate(FIELDS):
            if abs(a[i] - b[i]) > 1e-9:
                problems.append(f'{ts} {name}: live={a[i]} replay={b[i]}')
                break
    return problems


def check_historical(ex, now, symbol, timeframe, limit):
    """Compare finalized bars around `now` fetched live vs locally.

    Both sides are called with identical arguments so the windows align.
    The venue's in-progress bar is excluded from both sides.
    """
    span_ms = TIMEFRAME_MS[timeframe] * limit
    since = int((now - pd.Timedelta(milliseconds=span_ms)).timestamp() * 1000)

    live = _rows(ex.fetch_ohlcv(symbol, timeframe, since=since, limit=limit))
    rx = ReplayExchange(now, SYMBOLS)
    mine = _rows(rx.fetch_ohlcv(symbol, timeframe, since=since, limit=limit))

    # Exclude the in-progress bar: the venue's copy is live, ours is final.
    cutoff = now + pd.Timedelta(milliseconds=TIMEFRAME_MS[timeframe])
    live = {ts: v for ts, v in live.items() if ts < cutoff}
    mine = {ts: v for ts, v in mine.items() if ts < cutoff}

    return _diff(live, mine)


def check_current(ex, symbol, timeframe, limit):
    """Compare the live API against the replay, both as of the dataset cutoff.

    The local dataset stops at `cutoff`, but the venue keeps serving newer
    bars. Comparing the venue's *most recent* `limit` bars against the
    replay's would therefore compare two different windows (the venue's
    window is shifted forward by however many bars elapsed since the
    download).

    Instead we anchor BOTH sides to the cutoff using an explicit `since`,
    which makes the windows identical. The bar straddling the cutoff is the
    in-progress one at the venue: its timestamp and open are comparable,
    its high/low/close/volume are not (the venue reports them live, we hold
    the finalized values).
    """
    span_ms = TIMEFRAME_MS[timeframe] * limit
    since = int((cutoff - pd.Timedelta(milliseconds=span_ms)).timestamp() * 1000)

    live = _rows(ex.fetch_ohlcv(symbol, timeframe, since=since, limit=limit))
    rx = ReplayExchange(cutoff, SYMBOLS)
    mine = _rows(rx.fetch_ohlcv(symbol, timeframe, since=since, limit=limit))

    problems = []

    # The bar straddling the cutoff is in-progress at the venue.
    forming = None
    if live:
        last = max(live)
        if cutoff < last + pd.Timedelta(milliseconds=TIMEFRAME_MS[timeframe]):
            forming = last
            if forming not in mine:
                problems.append(f'forming bar {forming} missing from replay')
            elif abs(live[forming][0] - mine[forming][0]) > 1e-9:
                problems.append(
                    f'forming bar {forming} open: live={live[forming][0]} '
                    f'replay={mine[forming][0]}')
            live = {ts: v for ts, v in live.items() if ts != forming}
            mine = {ts: v for ts, v in mine.items() if ts != forming}

    problems.extend(_diff(live, mine))
    return problems, 0


def main():
    ex = ccxt.bybit({'options': {'defaultType': 'linear'}})
    now_real = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    global cutoff
    cutoff = dataset_cutoff()
    print('=== 1. cutoff-anchored window: replay vs live API ===')
    print(f'    (anchored at dataset cutoff {cutoff.isoformat()})')
    problems_total = 0
    checks = 0
    for symbol in SYMBOLS:
        for timeframe, limit in [('5m', 300), ('5m', 25), ('5m', 500),
                                 ('15m', 100), ('1d', 20)]:
            problems, _ = check_current(ex, symbol, timeframe, limit)
            checks += 1
            problems_total += len(problems)
            status = 'ok' if not problems else 'FAIL'
            print(f'  {status:4} {symbol} {timeframe} limit={limit}')
            for p in problems[:3]:
                print('       ', p)

    print('\n=== 2. historical instants: finalized bars vs local dataset ===')
    hist_instants = [
        now_real - timedelta(days=1),
        now_real - timedelta(days=3),
        now_real - timedelta(days=10),
        now_real - timedelta(days=45),
        now_real - timedelta(days=120),
        now_real - timedelta(days=300),
    ]
    for now in hist_instants:
        for symbol in SYMBOLS:
            for timeframe, limit in [('5m', 300), ('15m', 100), ('1d', 20)]:
                problems = check_historical(ex, now, symbol, timeframe, limit)
                checks += 1
                problems_total += len(problems)
                if problems:
                    print(f'  FAIL {now.date()} {symbol} {timeframe}')
                    for p in problems[:3]:
                        print('       ', p)
    print(f'  ({len(hist_instants) * len(SYMBOLS) * 3} historical comparisons)')

    print(f'\n{checks} comparisons, {problems_total} mismatches')
    return 0 if problems_total == 0 else 1


if __name__ == '__main__':
    sys.exit(main())
