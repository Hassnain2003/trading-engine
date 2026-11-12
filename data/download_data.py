#!/usr/bin/env python3
"""Stage 3.1 — Download 3+ years of mainnet public candles + funding from Bybit.

Public REST only (no API key). Endpoints:
  GET /v5/market/kline            (max 1000 candles/call)
  GET /v5/market/funding/history  (max 200 records/call, supports start/end)

Design notes
------------
* Deterministic chunking + fixed cutoff so re-runs are reproducible.
* Chunks are strictly smaller than the endpoint page limit so each chunk
  needs exactly one HTTP request (no intra-chunk pagination ambiguity).
* Downloads are resumable: existing chunk files are skipped.
* Retries with exponential backoff handle Bybit's transient rate limit
  (retCode 10006 = "too many visits") and network blips.
* Output: raw CSV per chunk, SHA-256 manifest, cutoff record, gap report.
"""
import hashlib
import json
import os
import random
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone, timedelta

BASE = "https://api.bybit.com/v5/market"
SYMBOLS = ["BTCUSDT", "ETHUSDT"]
# Bybit interval codes -> label
TIMEFRAMES = {"5": "5m", "15": "15m", "D": "1d"}
# chunk size in days, chosen so candles-per-chunk < 1000
CHUNK_DAYS = {"5": 3, "15": 10, "D": 365}
INTERVAL_MS = {"5": 5 * 60 * 1000, "15": 15 * 60 * 1000, "D": 86400000}
LOOKBACK_DAYS = 3 * 365  # 1095 days
RATE_SLEEP = 0.35         # min seconds between HTTP calls
MAX_RETRIES = 6
DATA_DIR = os.path.dirname(os.path.abspath(__file__))

# Errors that mean "slow down and retry"
RETRY_CODES = {10006, 10016, 10018}


def log(msg):
    print(msg, flush=True)


def fetch_json(path, params):
    """GET with retries/backoff. Returns the parsed Bybit response dict."""
    qs = "&".join(f"{k}={v}" for k, v in params.items())
    url = f"{BASE}/{path}?{qs}"
    last_err = None
    for attempt in range(MAX_RETRIES):
        if attempt:
            backoff = min(30.0, (2 ** attempt) + random.random())
            time.sleep(backoff)
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "research-harness/1.0"})
            with urllib.request.urlopen(req, timeout=60) as r:
                data = json.loads(r.read().decode())
        except Exception as e:  # network error
            last_err = f"{type(e).__name__}: {e}"
            continue

        code = data.get("retCode")
        if code == 0:
            time.sleep(RATE_SLEEP)
            return data
        msg = data.get("retMsg", "")
        last_err = f"retCode={code} retMsg={msg}"
        if code in RETRY_CODES:
            continue
        raise RuntimeError(f"{path} failed: {last_err} ({url})")
    raise RuntimeError(f"{path} failed after {MAX_RETRIES} retries: {last_err} ({url})")


def ms(dt):
    return int(dt.timestamp() * 1000)


def download_kline(symbol, interval, start_dt, end_dt, out_path):
    """One HTTP call. Chunks are sized so <1000 candles are returned."""
    data = fetch_json("kline", {
        "category": "linear",
        "symbol": symbol,
        "interval": interval,
        "start": ms(start_dt),
        "end": ms(end_dt),
        "limit": "1000",
    })
    lst = data.get("result", {}).get("list", [])
    with open(out_path, "w") as f:
        f.write("ts_ms,open,high,low,close,volume,turnover\n")
        for c in lst:
            f.write(",".join(c) + "\n")
    return len(lst)


def download_funding(symbol, start_dt, end_dt, out_path):
    """Page backward through funding history using startTime/endTime."""
    rows = {}
    cursor_end = end_dt
    while cursor_end > start_dt:
        data = fetch_json("funding/history", {
            "category": "linear",
            "symbol": symbol,
            "limit": "200",
            "startTime": ms(start_dt),
            "endTime": ms(cursor_end),
        })
        lst = data.get("result", {}).get("list", [])
        if not lst:
            break
        for r in lst:
            ts = int(r["fundingRateTimestamp"])
            dt = datetime.fromtimestamp(ts / 1000, tz=timezone.utc)
            if start_dt <= dt < end_dt:
                rows[ts] = (r["symbol"], r["fundingRate"], ts)
        oldest = min(int(r["fundingRateTimestamp"]) for r in lst)
        oldest_dt = datetime.fromtimestamp(oldest / 1000, tz=timezone.utc)
        if oldest_dt <= start_dt:
            break
        # next page ends just before the oldest record we just saw
        cursor_end = oldest_dt - timedelta(milliseconds=1)
        if cursor_end <= start_dt:
            break

    with open(out_path, "w") as f:
        f.write("symbol,funding_rate,funding_rate_timestamp_ms\n")
        for ts in sorted(rows):
            sym, rate, t = rows[ts]
            f.write(f"{sym},{rate},{t}\n")
    return len(rows)


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def chunk_name(symbol, label, start_dt, end_dt):
    return (f"{symbol}-USDT-USDT_{label}_"
            f"{start_dt.strftime('%Y%m%d')}_{end_dt.strftime('%Y%m%d')}.csv")


def main():
    os.makedirs(DATA_DIR, exist_ok=True)
    end_dt = datetime.now(tz=timezone.utc).replace(second=0, microsecond=0)
    start_dt = end_dt - timedelta(days=LOOKBACK_DAYS)
    log(f"Download window: {start_dt.isoformat()} -> {end_dt.isoformat()} ({LOOKBACK_DAYS} days)")
    log(f"Symbols: {SYMBOLS} | Timeframes: {list(TIMEFRAMES.values())} + funding")

    produced = []
    for symbol in SYMBOLS:
        for interval, label in TIMEFRAMES.items():
            step = timedelta(days=CHUNK_DAYS[interval])
            cs = start_dt
            while cs < end_dt:
                ce = min(cs + step, end_dt)
                fpath = os.path.join(DATA_DIR, chunk_name(symbol, label, cs, ce))
                if os.path.exists(fpath):
                    log(f"  {symbol} {label} {cs.date()}->{ce.date()}: SKIP")
                else:
                    n = download_kline(symbol, interval, cs, ce, fpath)
                    log(f"  {symbol} {label} {cs.date()}->{ce.date()}: {n} candles")
                produced.append(fpath)
                cs = ce

        fpath = os.path.join(DATA_DIR, chunk_name(symbol, "funding", start_dt, end_dt))
        if os.path.exists(fpath):
            log(f"  {symbol} funding: SKIP")
        else:
            n = download_funding(symbol, start_dt, end_dt, fpath)
            log(f"  {symbol} funding: {n} records")
        produced.append(fpath)

    # ---- checksum manifest (raw CSV only) ----
    csvs = sorted(p for p in produced if os.path.basename(p).endswith(".csv"))
    ck_path = os.path.join(DATA_DIR, "checksums.sha256")
    with open(ck_path, "w") as f:
        for fp in csvs:
            f.write(f"{sha256_file(fp)}  {os.path.basename(fp)}\n")
    log(f"Checksums -> {ck_path} ({len(csvs)} files)")

    # ---- cutoff record ----
    cutoff_path = os.path.join(DATA_DIR, "cutoff.txt")
    with open(cutoff_path, "w") as f:
        f.write(f"cutoff_utc={end_dt.isoformat()}\n")
        f.write(f"start_utc={start_dt.isoformat()}\n")
        f.write(f"lookback_days={LOOKBACK_DAYS}\n")
        f.write("source=bybit_public_rest_v5\n")
    log(f"Cutoff -> {cutoff_path}")

    # ---- gap report ----
    gap_path = os.path.join(DATA_DIR, "gap_report.txt")
    with open(gap_path, "w") as f:
        f.write("Gap report — candle continuity\n")
        f.write("=" * 66 + "\n")
        for symbol in SYMBOLS:
            for interval, label in TIMEFRAMES.items():
                files = [fp for fp in csvs
                         if os.path.basename(fp).startswith(f"{symbol}-USDT-USDT_{label}_")]
                ts_all = []
                for fp in files:
                    with open(fp) as cf:
                        next(cf)
                        for line in cf:
                            line = line.strip()
                            if line:
                                ts_all.append(int(line.split(",")[0]))
                ts_all = sorted(set(ts_all))
                gaps = []
                if ts_all:
                    for i in range(1, len(ts_all)):
                        d = ts_all[i] - ts_all[i - 1]
                        if d > INTERVAL_MS[interval] * 1.5:
                            gaps.append((ts_all[i - 1], ts_all[i], d))
                first = datetime.fromtimestamp(ts_all[0] / 1000, tz=timezone.utc).isoformat() if ts_all else "N/A"
                last = datetime.fromtimestamp(ts_all[-1] / 1000, tz=timezone.utc).isoformat() if ts_all else "N/A"
                f.write(f"\n{symbol} {label}\n")
                f.write(f"  candles: {len(ts_all)}  first: {first}  last: {last}\n")
                f.write(f"  gaps > 1.5x interval: {len(gaps)}\n")
                for a, b, d in gaps[:20]:
                    da = datetime.fromtimestamp(a / 1000, tz=timezone.utc).isoformat()
                    db = datetime.fromtimestamp(b / 1000, tz=timezone.utc).isoformat()
                    f.write(f"    {da} -> {db}  ({d / 3600000:.2f}h missing)\n")
                if len(gaps) > 20:
                    f.write(f"    ... and {len(gaps) - 20} more\n")
        # funding continuity
        f.write("\n" + "=" * 66 + "\nFunding record counts\n")
        for symbol in SYMBOLS:
            fp = os.path.join(DATA_DIR, chunk_name(symbol, "funding", start_dt, end_dt))
            if not os.path.exists(fp):
                continue
            rows = []
            with open(fp) as cf:
                next(cf)
                for line in cf:
                    line = line.strip()
                    if line:
                        rows.append(int(line.split(",")[2]))
            rows = sorted(set(rows))
            if not rows:
                f.write(f"  {symbol}: 0 records\n")
                continue
            gaps = [(rows[i - 1], rows[i]) for i in range(1, len(rows))
                    if rows[i] - rows[i - 1] > 8 * 3600 * 1000]
            f.write(f"  {symbol}: {len(rows)} records, first "
                    f"{datetime.fromtimestamp(rows[0]/1000, tz=timezone.utc).isoformat()}, "
                    f"last {datetime.fromtimestamp(rows[-1]/1000, tz=timezone.utc).isoformat()}, "
                    f"{len(gaps)} interval gaps\n")
        f.write(f"\nCutoff: {end_dt.isoformat()}\n")
    log(f"Gap report -> {gap_path}")
    log("Download complete.")


if __name__ == "__main__":
    main()
