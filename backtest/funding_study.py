"""Funding-rate edge study (Phase 2 research).

Hypothesis: perp funding rates encode crowd positioning. When funding is
extremely positive (crowded longs paying shorts), holding SHORT earns the
funding *and* tends to catch the mean reversion of an overheated market —
consistent with the P4 finding (fading +2σ pumps) in pattern_mining.py.

Method: same honest split as pattern_mining.py — first 2/3 of history is
DISCOVERY, last 1/3 VALIDATION, and only effects that hold in both count.

For each 8h funding interval we measure the NEXT interval's price return
conditioned on the current funding-rate bucket, plus the funding a short
would receive. Reported in bps per 8h; a live strategy needs the combined
edge to clear ~7 bps entry+exit costs amortized over the holding period.
"""

import sys
import os
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np
import pandas as pd
import ccxt

from backtest._common import DATA_DIR, fetch_ohlcv_cached

SYMBOLS = [
    "BTC/USDT:USDT", "ETH/USDT:USDT", "SOL/USDT:USDT", "BNB/USDT:USDT",
    "XRP/USDT:USDT", "LINK/USDT:USDT", "DOGE/USDT:USDT",
]
DAYS = 365
DISCOVERY_FRAC = 2 / 3


def fetch_funding_cached(symbol):
    safe = symbol.replace('/', '-').replace(':', '-')
    path = os.path.join(DATA_DIR, f"{safe}_funding_{DAYS}d.parquet")
    if os.path.exists(path):
        return pd.read_parquet(path)

    ex = ccxt.bybit({'enableRateLimit': True, 'options': {'defaultType': 'linear'}})
    until = ex.milliseconds()
    since = until - DAYS * 24 * 3600 * 1000
    rows = []
    while since < until:
        try:
            batch = ex.fetch_funding_rate_history(symbol, since=since, limit=200)
        except Exception as e:
            print(f"    funding fetch error {symbol}: {e}, retrying...")
            time.sleep(5)
            continue
        if not batch:
            break
        rows += batch
        since = batch[-1]['timestamp'] + 1
        time.sleep(ex.rateLimit / 1000)

    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame([{'ts': r['timestamp'], 'rate': float(r['fundingRate'])} for r in rows])
    df['ts'] = pd.to_datetime(df['ts'], unit='ms', utc=True)
    df = df.drop_duplicates(subset='ts').set_index('ts').sort_index()
    os.makedirs(DATA_DIR, exist_ok=True)
    df.to_parquet(path)
    print(f"    fetched+cached {len(df)} funding points for {symbol}")
    return df


def build_frame(symbol):
    """8h grid: funding rate + next-8h price return."""
    funding = fetch_funding_cached(symbol)
    prices = fetch_ohlcv_cached(symbol, '1h', DAYS)
    if funding.empty or prices.empty:
        return None
    px8 = prices['close'].resample('8h').last().dropna()
    ret_next = px8.pct_change().shift(-1)  # return over the NEXT 8h block
    f = funding['rate'].resample('8h').mean().reindex(px8.index).ffill()
    df = pd.DataFrame({'funding': f, 'ret_next': ret_next}).dropna()
    return df


def bucket_stats(df, label):
    """Condition next-8h return on funding buckets."""
    out = []
    buckets = [
        ('funding < -0.01%', df['funding'] < -0.0001),
        ('-0.01% .. +0.01%', (df['funding'] >= -0.0001) & (df['funding'] <= 0.0001)),
        ('+0.01% .. +0.03%', (df['funding'] > 0.0001) & (df['funding'] <= 0.0003)),
        ('funding > +0.03%', df['funding'] > 0.0003),
    ]
    for name, mask in buckets:
        sub = df[mask]
        if len(sub) < 15:
            out.append((name, None, None, len(sub)))
            continue
        px_bps = sub['ret_next'].mean() * 1e4
        fund_bps = sub['funding'].mean() * 1e4  # what a SHORT receives per 8h
        out.append((name, px_bps, fund_bps, len(sub)))
    return out


if __name__ == "__main__":
    frames = {}
    for sym in SYMBOLS:
        print(f"  loading {sym}...")
        frames[sym] = build_frame(sym)

    for window_name, lo, hi in [("DISCOVERY", 0.0, DISCOVERY_FRAC), ("VALIDATION", DISCOVERY_FRAC, 1.0)]:
        print(f"\n===== {window_name} — next-8h price return by funding bucket (pooled across symbols) =====")
        pooled = []
        for sym, df in frames.items():
            if df is None:
                continue
            n = len(df)
            pooled.append(df.iloc[int(n * lo):int(n * hi)])
        allf = pd.concat(pooled)
        for name, px_bps, fund_bps, n in bucket_stats(allf, window_name):
            if px_bps is None:
                print(f"  {name:22s} n={n} (insufficient)")
                continue
            # Short's total edge per 8h = -price move + funding received
            short_edge = -px_bps + fund_bps
            print(
                f"  {name:22s} px {px_bps:+7.1f} bps | funding(short recv) {fund_bps:+6.2f} bps | "
                f"SHORT edge {short_edge:+7.1f} bps/8h | n={n}"
            )
