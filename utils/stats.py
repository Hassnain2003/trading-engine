"""Trade statistics from agent_trades.db for AI jobs and gatekeeper."""

import os
import sqlite3
from datetime import datetime, timezone

from utils.config_loader import get as _cfg

# Bybit perpetuals taker fee (% of notional, per side). Round-trip = taker in +
# taker out. Env-overridable to match the fee tier the live account pays.
BYBIT_TAKER_PCT = float(os.getenv('BYBIT_TAKER_PCT', '0.055'))

# Slippage (% of notional, per side) — realistic demo/live slippage beyond
# taker fees. Configurable via config.yaml [slippage.bps_per_side] or env
# SLIPPAGE_BPS_PER_SIDE (e.g. 0.5 = 0.005% per side, 0.01% round trip).
SLIPPAGE_BPS_PER_SIDE = float(
    os.getenv('SLIPPAGE_BPS_PER_SIDE',
              str(_cfg('slippage', 'bps_per_side', '0.5')))
) / 10000.0  # basis points -> decimal

# Total round-trip cost: taker in + taker out + slippage in + slippage out.
ROUND_TRIP_COST_PCT = (BYBIT_TAKER_PCT * 2) + (SLIPPAGE_BPS_PER_SIDE * 2)

# Backtest baseline win rates — OUT-OF-SAMPLE numbers from the 2026-07 validation
# runs (see utils/live_tracker.py BASELINES for PF/avg baselines too). The old
# values here (S3 73% etc.) were from stale pre-relaxation backtests and wrong.
BASELINE_WR = {
    'strategy2_breakout_retest': 39.9,   # s2_r_multiple_walkforward.py OOS
    'strategy7_daily_momentum': 52.0,    # approx: daily continuation hit rate
}

# Strategies excluded from the go-live gate: none in the public copy
# (the sacred-harmonic S9 strategy was removed entirely — see README).
GATE_EXCLUDED_STRATEGIES = frozenset()


def _trade_pnl_pct(side, entry, exit_price):
    if not entry or not exit_price:
        return None
    if side == 'long':
        return (exit_price - entry) / entry * 100
    return (entry - exit_price) / entry * 100


def _loss_streak(pnls):
    streak = 0
    for p in reversed(pnls):
        if p < 0:
            streak += 1
        else:
            break
    return streak


def compute_strategy_stats(conn, day=None):
    """Per-strategy stats for today's (or given day) closed trades.

    Also includes funding_usdt: the sum of funding payments recorded for the
    strategy's symbols during that day. Funding is a real wallet-level cost/
    income that per-trade price math can't see (affects S9 harmonic shorts
    especially). Returns None for funding_usdt when no payments were recorded.
    """
    day = day or datetime.now(timezone.utc).strftime('%Y-%m-%d')
    c = conn.cursor()
    # Prefer the ledger-backed columns; fall back gracefully when the schema
    # predates Stage 1.2 (e.g. archived DBs or minimal test fixtures).
    try:
        c.execute(
            """SELECT strategy, side, entry, exit_price, symbol,
                      net_pnl_usd, entry_fee, exit_fee
               FROM signals
               WHERE status='closed' AND timestamp LIKE ? AND exit_price IS NOT NULL""",
            (f"{day}%",),
        )
        rows = c.fetchall()
    except sqlite3.OperationalError:
        c.execute(
            """SELECT strategy, side, entry, exit_price, symbol
               FROM signals
               WHERE status='closed' AND timestamp LIKE ? AND exit_price IS NOT NULL""",
            (f"{day}%",),
        )
        rows = [(*r, None, None, None) for r in c.fetchall()]

    by_strategy = {}
    strategy_symbols = {}
    agg_stats = {}
    for strategy, side, entry, exit_price, symbol, net_pnl, e_fee, x_fee in rows:
        pnl = _trade_pnl_pct(side, entry, exit_price)
        if pnl is None:
            continue
        by_strategy.setdefault(strategy, []).append(pnl)
        strategy_symbols.setdefault(strategy, set()).add(symbol)
        # Track measured $ net P&L and fees alongside the %-based series.
        rec = agg_stats.setdefault(strategy, {'net': 0.0, 'net_n': 0,
                                              'fees': 0.0, 'fees_n': 0})
        if net_pnl is not None:
            rec['net'] += net_pnl
            rec['net_n'] += 1
        if e_fee is not None or x_fee is not None:
            rec['fees'] += (e_fee or 0.0) + (x_fee or 0.0)
            rec['fees_n'] += 1

    stats = {}
    for strategy, pnls in by_strategy.items():
        wins = sum(1 for p in pnls if p > 0)
        rec = agg_stats.get(strategy, {})
        stats[strategy] = {
            'pnl': round(sum(pnls), 2),
            'wr': round(wins / len(pnls) * 100, 1) if pnls else 0.0,
            'loss_streak': _loss_streak(pnls),
            'baseline_wr': BASELINE_WR.get(strategy, 50.0),
            'trades': len(pnls),
            'funding_usdt': _funding_for_day(conn, day, strategy_symbols.get(strategy, set())),
            # Measured (fee-inclusive) net P&L in USDT; None until closes
            # populate net_pnl_usd from the ledger.
            'net_pnl_usd': round(rec['net'], 2) if rec.get('net_n') else None,
            'fees_usdt': round(rec['fees'], 4) if rec.get('fees_n') else None,
        }
    return stats


def _funding_for_day(conn, day, symbols):
    """Sum funding payments for the given symbols on the given day (UTC).

    Returns the total amount (USDT) or None if no payments / no symbols.
    """
    if not symbols:
        return None
    try:
        placeholders = ','.join('?' * len(symbols))
        c = conn.cursor()
        c.execute(
            f"""SELECT COALESCE(SUM(amount), 0), COUNT(*)
                FROM funding_payments
                WHERE timestamp LIKE ? AND symbol IN ({placeholders})""",
            (f"{day}%", *symbols),
        )
        total, n = c.fetchone()
        return round(total, 4) if n else None
    except Exception:
        return None


def live_config_scope():
    """Authoritative {(strategy_module, symbol)} the engine trades right now.

    Sourced lazily from execution.engine so there is a single source of truth and
    no drift. Lazy (not a top-level import) for two reasons: (1) execution.engine
    imports utils.stats, so a top-level import here would be circular; (2) it pulls
    in ccxt / the execution tree, which the gate should not pay for unless scoping
    is actually requested. Returns None if the config cannot be read, so the caller
    degrades to counting every (venue-filtered) trade rather than silently
    reporting an empty gate.
    """
    try:
        from execution.engine import STRATEGY_CONFIG, ACTIVE_STRATEGIES
    except Exception:
        return None
    return {(mod, coin) for mod, coins in STRATEGY_CONFIG
            if mod in ACTIVE_STRATEGIES for coin in coins}


def compute_demo_metrics(conn, net_of_cost=True, scope_to_live_config=True):
    """Aggregate demo metrics for the go-live gatekeeper.

    Two correctness knobs, both ON by default (2026-08-25):

    * net_of_cost — subtract round-trip taker (ROUND_TRIP_COST_PCT) from each
      trade's % return, so live metrics compare apples-to-apples with the
      after-cost out-of-sample backtests the GATE_CRITERIA were calibrated
      against. Funding is deliberately NOT folded in: it is a wallet-level $
      effect that does not map cleanly onto a per-trade %, and this gate is
      sizing-neutral %-only by design — $ P&L (funding included) is P4
      attribution, reported phase-scoped by utils.live_tracker.

    * scope_to_live_config — count only trades on (strategy, coin) pairs the
      engine trades today. Without it, retired coins (e.g. the S7 alts
      XRP/BNB/DOGE cut 2026-08-25) sit in the gate forever and understate the
      go-forward book. Parameter changes within a still-live coin are NOT scoped
      out — that deeper regime problem is left for later.

    Broken-demo-venue symbols are always excluded — see
    utils.live_tracker.EXCLUDED_SYMBOLS (fake-price fills would poison the gate).
    """
    from utils.live_tracker import EXCLUDED_SYMBOLS
    c = conn.cursor()
    c.execute(
        f"""SELECT strategy, symbol, side, entry, exit_price
           FROM signals
           WHERE status='closed' AND exit_price IS NOT NULL
           AND symbol NOT IN ({','.join('?' * len(EXCLUDED_SYMBOLS))})
           ORDER BY timestamp""",
        tuple(EXCLUDED_SYMBOLS),
    )
    rows = c.fetchall()

    scope = live_config_scope() if scope_to_live_config else None
    cost = ROUND_TRIP_COST_PCT if net_of_cost else 0.0

    pnls = []
    out_of_scope = 0
    for strategy, symbol, side, entry, exit_price in rows:
        if scope is not None and (strategy, symbol) not in scope:
            out_of_scope += 1
            continue
        # 2.6: gate-excluded strategies (S9 on demo, execution-testing only)
        # never count toward the go-live gate.
        if strategy in GATE_EXCLUDED_STRATEGIES:
            out_of_scope += 1
            continue
        p = _trade_pnl_pct(side, entry, exit_price)
        if p is not None:
            pnls.append(p - cost)

    meta = {
        'net_of_cost': net_of_cost,
        'round_trip_cost_pct': round(cost, 4),
        'scoped_to_live_config': scope is not None,
        'out_of_scope_trades': out_of_scope,
    }

    if not pnls:
        return {
            'total_trades': 0,
            'win_rate_pct': 0.0,
            'profit_factor': 0.0,
            'max_drawdown_pct': 0.0,
            'sharpe_ratio': 0.0,
            'avg_pnl_pct': 0.0,
            **meta,
        }

    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    gross_profit = sum(wins) if wins else 0.0
    gross_loss = abs(sum(losses)) if losses else 0.0
    pf = gross_profit / gross_loss if gross_loss > 0 else (999.0 if gross_profit > 0 else 0.0)

    equity = 100.0
    peak = equity
    max_dd = 0.0
    for p in pnls:
        equity *= (1 + p / 100)
        peak = max(peak, equity)
        dd = (peak - equity) / peak * 100
        max_dd = max(max_dd, dd)

    import math
    mean = sum(pnls) / len(pnls)
    variance = sum((p - mean) ** 2 for p in pnls) / len(pnls)
    std = math.sqrt(variance) if variance > 0 else 0.0
    sharpe = (mean / std) * math.sqrt(len(pnls)) if std > 0 else 0.0

    return {
        'total_trades': len(pnls),
        'win_rate_pct': round(len(wins) / len(pnls) * 100, 1),
        'profit_factor': round(pf, 2),
        'max_drawdown_pct': round(max_dd, 1),
        'sharpe_ratio': round(sharpe, 2),
        'avg_pnl_pct': round(mean, 3),
        **meta,
    }


# Go-live gate thresholds (mirrors claude/jobs.go_live_gatekeeper)
GATE_CRITERIA = {
    'profit_factor': ('>', 1.3),
    'sharpe_ratio': ('>', 1.0),
    'max_drawdown_pct': ('<', 15.0),
    'total_trades': ('>=', 60),
}


def gate_status(conn):
    """Current go-live gate state on net-of-cost, live-config-scoped trades
    (venue-filtered). See compute_demo_metrics for the two correctness knobs."""
    m = compute_demo_metrics(conn)
    checks = {}
    for key, (op, threshold) in GATE_CRITERIA.items():
        val = m[key]
        ok = (val > threshold if op == '>' else
              val < threshold if op == '<' else val >= threshold)
        checks[key] = {'value': val, 'op': op, 'threshold': threshold, 'ok': bool(ok)}
    return {
        'metrics': m,
        'checks': checks,
        'passed': all(c['ok'] for c in checks.values()),
        'trades_progress': min(m['total_trades'] / 60, 1.0),
    }


def gate_summary_line(conn):
    g = gate_status(conn)
    parts = []
    for key, c in g['checks'].items():
        mark = '✅' if c['ok'] else '✗'
        parts.append(f"{key.replace('_', ' ')}: {c['value']} {mark}")
    status = 'PASSED 🚀' if g['passed'] else f"{g['metrics']['total_trades']}/60 trades"
    return f"🎯 Go-live gate [{status}] — " + " | ".join(parts)
