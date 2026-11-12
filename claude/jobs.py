import json
import logging
from datetime import datetime, timezone
from .gemini_client import ask_gemini_or_fallback
from .notifier import send_alert
from .config import MAX_DD_INTRADAY_PCT, CORRELATION_THRESHOLD

logger = logging.getLogger('claude_jobs')

def pre_session_scan(market_data):
    system = "You are a professional crypto risk manager. Analyze the pre‑session data and assign each coin a status: ACTIVE, REDUCED, or DISABLED. Provide a short justification for each. End with a 3‑line summary for a trader."
    prompt = f"Pre‑session market scan at {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')} UTC:\n"
    for coin, data in market_data.items():
        prompt += f"{coin}: price={data.get('price')}, 24h change={data.get('change')}%, vol={data.get('volume')}, spread={data.get('spread')}, funding={data.get('funding_rate')}, vol_vs_30d={data.get('volatility_vs_30d')}, btc_dom_shift={data.get('btc_dominance_shift')}\n"
    prompt += "\nReturn your analysis as a JSON object like {\"BTC\": {\"status\": \"ACTIVE\", \"reason\": \"...\"}, ...}."
    fallback = json.dumps({
        coin: {"status": "ACTIVE", "reason": "Gemini unavailable — no AI adjustment applied"}
        for coin in market_data
    })
    response = ask_gemini_or_fallback(system, prompt, fallback, max_tokens=1024)
    send_alert(f"📊 *Pre‑session Scan*\n{response[:4000]}")
    logger.info(f"Pre‑session recommendation: {response}")
    return response

def intraday_drawdown_check(initial_equity, current_equity, open_positions):
    dd_pct = (current_equity - initial_equity) / initial_equity * 100
    if dd_pct <= -MAX_DD_INTRADAY_PCT:
        msg = f"🚨 *Intraday Drawdown Alert* 🚨\nAccount DD: {dd_pct:.2f}%\nOpen positions: {open_positions}\nTime: {datetime.now(timezone.utc).isoformat()}"
        send_alert(msg)
        return True
    return False

def _daily_brief_data(conn):
    """Equity day-change, funding, and open positions for the analyst brief."""
    if conn is None:
        return ""
    lines = []
    today = datetime.now(timezone.utc).strftime('%Y-%m-%d')
    try:
        row = conn.execute(
            "SELECT MIN(equity), MAX(equity) FROM equity_snapshots WHERE timestamp LIKE ?",
            (f"{today}%",),
        ).fetchone()
        first = conn.execute(
            "SELECT equity FROM equity_snapshots WHERE timestamp LIKE ? ORDER BY id LIMIT 1",
            (f"{today}%",),
        ).fetchone()
        last = conn.execute(
            "SELECT equity FROM equity_snapshots WHERE timestamp LIKE ? ORDER BY id DESC LIMIT 1",
            (f"{today}%",),
        ).fetchone()
        if first and last:
            chg = (last[0] - first[0]) / first[0] * 100
            lines.append(
                f"Equity today: {first[0]:.0f} -> {last[0]:.0f} USDT ({chg:+.2f}%), "
                f"day range [{row[0]:.0f}, {row[1]:.0f}]"
            )
    except Exception:
        pass
    try:
        f = conn.execute(
            "SELECT COALESCE(SUM(amount),0), COUNT(*) FROM funding_payments WHERE timestamp LIKE ?",
            (f"{today}%",),
        ).fetchone()
        lines.append(f"Funding today: {f[0]:+.2f} USDT over {f[1]} payments")
    except Exception:
        pass
    try:
        rows = conn.execute(
            "SELECT symbol, strategy, side, entry FROM signals WHERE status='open'"
        ).fetchall()
        if rows:
            pos = ", ".join(f"{r[2]} {r[0].split('/')[0]} ({r[1].split('_')[0]})" for r in rows)
            lines.append(f"Open positions: {pos}")
    except Exception:
        pass
    return "\n".join(lines)


def post_session_review(strategy_stats, trades_today, tracker_report=None, conn=None):
    system = (
        "You are the performance analyst for a systematic crypto trading desk. "
        "Write a concise end-of-day brief: 1) day P&L context, 2) whether each live "
        "strategy is behaving in line with its validated baseline, 3) funding cost "
        "impact, 4) any anomaly that deserves the trader's attention tomorrow. "
        "Plain language, bullet points, no filler."
    )
    prompt = f"End-of-day data, {datetime.now(timezone.utc).strftime('%Y-%m-%d')} UTC:\n"
    prompt += f"Trades opened today: {trades_today}\n"
    if not strategy_stats:
        prompt += "No closed trades with P&L recorded today.\n"
    for strat, stats in strategy_stats.items():
        prompt += f"{strat}: P&L={stats['pnl']:.2f}%, Win Rate={stats['wr']:.1f}%, Consecutive Losses={stats['loss_streak']}, Baseline WR={stats['baseline_wr']:.1f}%, Closed={stats.get('trades', 0)}\n"
    brief = _daily_brief_data(conn)
    if brief:
        prompt += f"\nAccount status:\n{brief}\n"
    if tracker_report:
        prompt += f"\nCumulative live-vs-backtest comparison:\n{tracker_report}\n"
    fallback_lines = [f"Trades opened today: {trades_today}"]
    for strat, stats in strategy_stats.items():
        fallback_lines.append(
            f"{strat}: P&L={stats['pnl']:.2f}%, WR={stats['wr']:.1f}%, "
            f"loss streak={stats['loss_streak']}, baseline WR={stats['baseline_wr']:.1f}%"
        )
    if brief:
        fallback_lines.append(brief)
    fallback = "(Gemini unavailable — rule-based brief)\n" + "\n".join(fallback_lines)
    response = ask_gemini_or_fallback(system, prompt, fallback, max_tokens=700)
    msg = f"🌙 *Daily Analyst Brief*\n{response[:2600]}"
    if tracker_report:
        msg += f"\n\n{tracker_report[:900]}"
    if conn is not None:
        try:
            from utils.stats import gate_summary_line
            msg += f"\n\n{gate_summary_line(conn)}"
        except Exception:
            pass
    send_alert(msg)
    logger.info(f"Post-session review: {response}")
    return response

def weekly_correlation_check(correlation_matrix):
    high_corr_pairs = []
    for pair, value in correlation_matrix.stack().items():
        if value > CORRELATION_THRESHOLD and pair[0] != pair[1]:
            high_corr_pairs.append((pair[0], pair[1], value))
    if high_corr_pairs:
        msg = f"🔗 *Weekly Correlation Alert*\nHigh correlations detected:\n" + "\n".join([f"{a}‑{b}: {v:.2f}" for a,b,v in high_corr_pairs])
        msg += "\nRecommendation: Reduce max concurrent trades to 2 until correlation drops."
        send_alert(msg)
        logger.info(msg)
        return True
    send_alert("✅ *Weekly Correlation Check*: All pairs within safe limits.")
    return False

def _deterministic_gate_verdict(metrics):
    """Rule-based GO/NO-GO from the same thresholds as utils.stats.GATE_CRITERIA —
    authoritative when Gemini is down, and shown next to its opinion when up."""
    from utils.stats import GATE_CRITERIA
    checks, passed = [], True
    for key, (op, threshold) in GATE_CRITERIA.items():
        val = metrics.get(key)
        if val is None:
            checks.append(f"{key.replace('_', ' ')}: n/a ✗")
            passed = False
            continue
        ok = (val > threshold if op == '>'
              else val < threshold if op == '<'
              else val >= threshold)
        passed = passed and ok
        checks.append(f"{key.replace('_', ' ')} {val} {op} {threshold} {'✅' if ok else '✗'}")
    verdict = 'GO' if passed else 'NO-GO'
    return f"{verdict} (rule-based) — " + " | ".join(checks)


def go_live_gatekeeper(metrics):
    fallback = _deterministic_gate_verdict(metrics)
    system = "You are a gatekeeper for a live trading system. Based on the following demo metrics, provide a clear GO/NO‑GO decision with reasons."
    prompt = "Go‑Live Gate Metrics:\n"
    for k, v in metrics.items():
        prompt += f"{k}: {v}\n"
    prompt += "\nAre all criteria met (PF>1.3, Sharpe>1.0, MaxDD<15%, Trades>=60)? Reply with GO or NO‑GO and a brief explanation."
    response = ask_gemini_or_fallback(system, prompt, fallback, max_tokens=512)
    if response == fallback:
        msg = f"🔐 *Live‑Switch Decision*: {fallback}"
    else:
        msg = f"🔐 *Live‑Switch Decision*: {fallback}\n\nAnalyst view: {response[:3500]}"
    send_alert(msg)
    logger.info(f"Go‑live decision: {response} | rule-based: {fallback}")
    return response
