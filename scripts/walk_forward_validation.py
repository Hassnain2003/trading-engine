#!/usr/bin/env python3
"""Walk-forward validation of live strategy performance.

Splits the closed-trade journal into rolling train/test windows, scores
each strategy on the train window, and evaluates out-of-sample on the
following test window. This prevents overfitting to a single window and
answers: "is the live edge persistent or was it a lucky regime?"

Usage:
    python scripts/walk_forward_validation.py [--train-days 30] [--test-days 7]

Requires: agent_trades_<phase>.db (the current phase journal).
"""

import argparse
import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from trading_phase import DB_FILENAME

MIN_TRADES_PER_FOLD = 5


def load_trades(db_path, strategy=None):
    """Load all closed trades with entry/exit timestamps from the DB."""
    conn = sqlite3.connect(db_path)
    q = ("SELECT timestamp, strategy, symbol, side, entry, exit_price "
         "FROM signals WHERE status='closed' AND exit_price IS NOT NULL")
    params = []
    if strategy:
        q += " AND strategy=?"
        params.append(strategy)
    q += " ORDER BY timestamp"
    rows = conn.execute(q, params).fetchall()
    conn.close()

    trades = []
    for ts, strat, sym, side, entry, xp in rows:
        if not entry or not xp:
            continue
        if side == 'long':
            pnl_pct = (xp - entry) / entry * 100
        else:
            pnl_pct = (entry - xp) / entry * 100
        trades.append({
            'timestamp': ts,
            'strategy': strat,
            'symbol': sym,
            'pnl_pct': pnl_pct,
        })
    return trades


def compute_metrics(pnls):
    """PF, WR, avg P&L, trade count from a list of % returns."""
    if not pnls:
        return None
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    gross_profit = sum(wins)
    gross_loss = abs(sum(losses))
    pf = (gross_profit / gross_loss) if gross_loss > 0 else (999.0 if wins else 0.0)
    return {
        'trades': len(pnls),
        'wr': len(wins) / len(pnls) * 100 if pnls else 0,
        'pf': pf,
        'avg_pnl_pct': sum(pnls) / len(pnls) if pnls else 0,
        'total_pnl_pct': sum(pnls),
    }


def walk_forward(trades, train_days, test_days):
    """Rolling walk-forward: train on N days, test on next M days, roll forward.

    Returns a list of fold results. Each fold: {'train': metrics, 'test': metrics}.
    """
    if not trades:
        return []

    # Parse timestamps
    for t in trades:
        t['dt'] = datetime.fromisoformat(t['timestamp'].replace('Z', '+00:00'))

    trades.sort(key=lambda t: t['dt'])
    start = trades[0]['dt']
    end = trades[-1]['dt']

    folds = []
    window_start = start
    while True:
        train_end = window_start + timedelta(days=train_days)
        test_end = train_end + timedelta(days=test_days)
        if test_end > end + timedelta(days=1):
            break

        train_pnls = [t['pnl_pct'] for t in trades
                      if window_start <= t['dt'] < train_end]
        test_pnls = [t['pnl_pct'] for t in trades
                     if train_end <= t['dt'] < test_end]

        if len(train_pnls) >= MIN_TRADES_PER_FOLD and len(test_pnls) >= 1:
            folds.append({
                'train_start': window_start.strftime('%Y-%m-%d'),
                'train_end': train_end.strftime('%Y-%m-%d'),
                'test_end': test_end.strftime('%Y-%m-%d'),
                'train': compute_metrics(train_pnls),
                'test': compute_metrics(test_pnls),
            })
        window_start += timedelta(days=test_days)  # roll forward by test window

    return folds


def run():
    parser = argparse.ArgumentParser(description='Walk-forward validation')
    parser.add_argument('--db', default=DB_FILENAME)
    parser.add_argument('--train-days', type=int, default=30)
    parser.add_argument('--test-days', type=int, default=7)
    parser.add_argument('--strategy', default=None,
                        help='Only validate this strategy module name')
    args = parser.parse_args()

    db_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), args.db)
    if not os.path.exists(db_path):
        print(f"ERROR: DB not found: {db_path}")
        sys.exit(1)

    trades = load_trades(db_path, strategy=args.strategy)
    print(f"Loaded {len(trades)} closed trades from {args.db}")
    if not trades:
        print("No closed trades to validate.")
        return

    strategies = sorted({t['strategy'] for t in trades})
    print(f"Strategies: {', '.join(strategies)}")
    print(f"Window: train {args.train_days}d / test {args.test_days}d\n")

    for strat in strategies:
        strat_trades = [t for t in trades if t['strategy'] == strat]
        print(f"=== {strat} ({len(strat_trades)} trades) ===")
        folds = walk_forward(strat_trades, args.train_days, args.test_days)
        if not folds:
            print("  Not enough data for walk-forward folds\n")
            continue

        all_test_metrics = []
        for i, f in enumerate(folds):
            tr, te = f['train'], f['test']
            print(f"  Fold {i+1}: train {f['train_start']}→{f['train_end']} "
                  f"({tr['trades']}tr PF {tr['pf']:.2f} WR {tr['wr']:.0f}%)")
            print(f"           test  →{f['test_end']} "
                  f"({te['trades']}tr PF {te['pf']:.2f} WR {te['wr']:.0f}% avg {te['avg_pnl_pct']:+.3f}%)")
            all_test_metrics.append(te)

        if all_test_metrics:
            total_trades = sum(m['trades'] for m in all_test_metrics)
            avg_pf = sum(m['pf'] for m in all_test_metrics) / len(all_test_metrics)
            avg_wr = sum(m['wr'] for m in all_test_metrics) / len(all_test_metrics)
            avg_pnl = sum(m['avg_pnl_pct'] * m['trades'] for m in all_test_metrics) / total_trades
            verdict = "✅ edge holds out-of-sample" if avg_pf >= 1.0 else "⚠️ edge does NOT hold"
            print(f"\n  Pooled OOS ({len(folds)} folds, {total_trades} trades): "
                  f"avg PF {avg_pf:.2f}, WR {avg_wr:.1f}%, avg {avg_pnl:+.3f}%/trade")
            print(f"  → {verdict}\n")
        else:
            print("  No test folds produced\n")


if __name__ == '__main__':
    run()
