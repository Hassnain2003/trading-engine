"""Funding-data statistics for the Stage 3 replay (non-interactive).

Reports per-symbol funding cadence, magnitude distribution, and the
fraction of negative funding intervals, so the harness can decide how to
apply funding cashflows deterministically.
"""
import csv
import glob
import os
from collections import Counter
from datetime import datetime, timezone

import numpy as np

DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'data')


def load_funding(path):
    rows = []
    with open(path, newline='') as fh:
        for rec in csv.DictReader(fh):
            rows.append((int(rec['funding_rate_timestamp_ms']), float(rec['funding_rate'])))
    rows.sort()
    return rows


def main():
    for path in sorted(glob.glob(os.path.join(DATA_DIR, '*_funding_*.csv'))):
        sym = os.path.basename(path).split('_')[0]
        rows = load_funding(path)
        ts = np.array([r[0] for r in rows], dtype=np.int64)
        rates = np.array([r[1] for r in rows], dtype=float)
        gaps = np.diff(ts) / 3_600_000.0
        gap_counts = Counter(np.round(gaps, 3).tolist())
        q = np.percentile(rates, [0, 1, 5, 25, 50, 75, 95, 99, 100])
        print(f'== {sym} ==')
        print(f'  rows              : {len(rows)}')
        print(f'  first             : {datetime.fromtimestamp(ts[0]/1000, timezone.utc)}')
        print(f'  last              : {datetime.fromtimestamp(ts[-1]/1000, timezone.utc)}')
        print(f'  gap hours (counts): {dict(sorted(gap_counts.items()))}')
        print(f'  rate min/p1/p5/p25/med/p75/p95/p99/max: '
              + ' '.join(f'{v:+.6f}' for v in q))
        print(f'  mean              : {rates.mean():+.6f}  '
              f'abs mean: {np.abs(rates).mean():.6f}')
        print(f'  negative fraction : {(rates < 0).mean():.4f}   '
              f'zero fraction: {(rates == 0).mean():.4f}')
        print(f'  >|0.01%| fraction : {(np.abs(rates) > 0.0001).mean():.4f}')
        print(f'  distinct values   : {len(set(rates.tolist()))}')
        top = Counter(rates.tolist()).most_common(5)
        print(f'  most common rates : {top}')
        # Annualised cost of a perpetual long holding through the worst rate.
        worst = rates.min()
        print(f'  worst interval    : {worst:+.6f} '
              f'-> 3x daily = {worst*3*365:+.2%}/yr on notional')
        print()


if __name__ == '__main__':
    main()
