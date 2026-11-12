"""Live-vs-backtest comparison — the feedback loop that says whether the
validated edges are actually showing up in live trading.

Baselines are the OUT-OF-SAMPLE numbers from the validation runs (never the
flattering in-sample ones):
  - S2: backtest/s2_r_multiple_walkforward.py — OOS PF 1.17, WR 39.9%,
        avg +0.113%/trade (SOL+BNB; BTC separately OOS PF 1.61).
  - S7: backtest/p1_daily_momentum_strategy.py — validation-window PF 1.20,
        avg +0.130%/day-held (per flip-to-flip trade ≈ per day).
"""

from datetime import datetime, timezone

BASELINES = {
    # S2 baseline recomputed 2026-07-11 for its actual live symbols
    # (BNB+BTC pooled OOS walk-forward, 98 trades) after SOL's demo-venue
    # exclusion made the old SOL+BNB baseline apples-to-oranges.
    'strategy2_breakout_retest': {'pf': 1.37, 'wr': 38.8, 'avg_pnl_pct': 0.245},
    'strategy7_daily_momentum': {'pf': 1.20, 'wr': None, 'avg_pnl_pct': 0.130},
}

MIN_TRADES_FOR_VERDICT = 15

# Broken-demo-venue symbols: their fills/exits are fake-price artifacts
# (LINK book abandoned; SOL printed 71-118 while trading ~78). Their rows
# would poison PF/WR with losses no real venue would produce.
EXCLUDED_SYMBOLS = ("SOL/USDT:USDT", "LINK/USDT:USDT")


def _trade_pnl_pct(side, entry, exit_price):
    if not entry or not exit_price:
        return None
    if str(side).lower() in ('long', 'buy'):
        return (exit_price - entry) / entry * 100
    return (entry - exit_price) / entry * 100


def live_stats_by_strategy(conn, since=None):
    """All-time (or since ISO date) per-strategy stats from closed trades."""
    c = conn.cursor()
    q = ("SELECT strategy, side, entry, exit_price FROM signals "
         "WHERE status='closed' AND exit_price IS NOT NULL "
         f"AND symbol NOT IN ({','.join('?' * len(EXCLUDED_SYMBOLS))})")
    params = tuple(EXCLUDED_SYMBOLS)
    if since:
        q += " AND timestamp >= ?"
        params = params + (since,)
    c.execute(q, params)

    by_strategy = {}
    for strategy, side, entry, exit_price in c.fetchall():
        pnl = _trade_pnl_pct(side, entry, exit_price)
        if pnl is not None:
            by_strategy.setdefault(strategy, []).append(pnl)

    out = {}
    for strategy, pnls in by_strategy.items():
        wins = [p for p in pnls if p > 0]
        losses = [p for p in pnls if p <= 0]
        gross_loss = abs(sum(losses))
        pf = (sum(wins) / gross_loss) if gross_loss > 0 else (999.0 if wins else 0.0)
        out[strategy] = {
            'trades': len(pnls),
            'wr': len(wins) / len(pnls) * 100,
            'pf': pf,
            'avg_pnl_pct': sum(pnls) / len(pnls),
        }
    return out


def _verdict(live, base):
    if live['trades'] < MIN_TRADES_FOR_VERDICT:
        return f"COLLECTING ({live['trades']}/{MIN_TRADES_FOR_VERDICT} trades)"
    if live['avg_pnl_pct'] <= 0:
        return "⚠️ BELOW — live expectancy negative, review before scaling"
    if base and live['pf'] < base['pf'] * 0.6:
        return "⚠️ LAGGING backtest — watch closely"
    if base and live['pf'] >= base['pf']:
        return "✅ ON/ABOVE backtest"
    return "✅ positive, tracking toward baseline"


def build_report(conn, since=None):
    """Human-readable live-vs-backtest report (also used in daily alerts)."""
    stats = live_stats_by_strategy(conn, since=since)
    lines = ["📈 Live vs Backtest (closed trades)"]
    for strategy, base in BASELINES.items():
        live = stats.get(strategy)
        if not live:
            lines.append(f"{strategy}: no closed trades yet")
            continue
        wr_base = f"{base['wr']:.0f}%" if base['wr'] is not None else "n/a"
        lines.append(
            f"{strategy}: {live['trades']} trades | "
            f"WR {live['wr']:.1f}% (bt {wr_base}) | "
            f"PF {live['pf']:.2f} (bt {base['pf']:.2f}) | "
            f"avg {live['avg_pnl_pct']:+.3f}%/trade (bt {base['avg_pnl_pct']:+.3f}%)"
        )
        lines.append(f"  → {_verdict(live, base)}")
    # Anything actively trading without a baseline is a red flag; ignore
    # historical trades from strategies that have since been disabled.
    c = conn.cursor()
    c.execute(
        "SELECT DISTINCT strategy FROM signals WHERE timestamp >= datetime('now', '-3 days') "
        "AND status IN ('open', 'pending', 'closed')"
    )
    recent = {row[0] for row in c.fetchall()}
    for strategy in stats:
        if strategy not in BASELINES and strategy in recent:
            lines.append(f"⚠️ {strategy}: trading without a validated baseline!")

    # Funding is a real S7 cost/income the per-trade price math can't see.
    # Scope to the active phase: funding_payments still holds the prior ~$34k
    # phase's carry (four BTC payments ≈ +$44 dated Jul 31–Aug 1), which
    # summed onto the $50 book read as an impossible +$46. Real micro-phase
    # funding is ≈ +$1. Boundary uses the 19-char T-prefix so the DB's
    # ".000Z" and the constant's "+00:00" suffixes compare correctly.
    try:
        from trading_phase import phase_started_dt
        boundary = phase_started_dt().strftime('%Y-%m-%dT%H:%M:%S')
        c.execute(
            "SELECT COALESCE(SUM(amount), 0), COUNT(*) FROM funding_payments "
            "WHERE timestamp >= ?",
            (boundary,),
        )
        total, n = c.fetchone()
        if n:
            lines.append(f"💸 Funding P&L this phase: {total:+.2f} USDT across {n} payments")
    except Exception:
        pass
    return "\n".join(lines)
