import logging
from datetime import datetime, timezone

from utils.coin_status import parse_pre_session_status
from .jobs import (
    pre_session_scan,
    intraday_drawdown_check,
    post_session_review,
    weekly_correlation_check,
)
from .config import (
    PRE_SESSION_TIME,
    POST_SESSION_TIME,
    INTRADAY_CHECK_INTERVAL,
)
from .notifier import send_alert

logger = logging.getLogger('claude_scheduler')

_last_intraday_check = 0
WEEKLY_CORRELATION_START_HOUR = 6  # Sunday >= 06:00 UTC


def _safe_run(label, fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except Exception as e:
        logger.error(f"{label} failed: {e}", exc_info=True)
        return None


def _kv_get(agent_state, key):
    conn = agent_state.get('conn')
    if conn is None:
        return None
    try:
        row = conn.execute("SELECT value FROM kv_state WHERE key=?", (key,)).fetchone()
        return row[0] if row else None
    except Exception:
        return None


def _kv_set(agent_state, key, value):
    conn = agent_state.get('conn')
    if conn is None:
        return
    try:
        conn.execute(
            "INSERT INTO kv_state (key, value) VALUES (?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, str(value)),
        )
        conn.commit()
    except Exception:
        pass


def _job_done_today(agent_state, job, now):
    return _kv_get(agent_state, f"{job}_done_date") == now.strftime('%Y-%m-%d')


def _mark_job_done(agent_state, job, now):
    _kv_set(agent_state, f"{job}_done_date", now.strftime('%Y-%m-%d'))


def _utc_at_or_after(now, hhmm):
    """True if now's clock time is at or past HH:MM (UTC)."""
    hour, minute = map(int, hhmm.split(':'))
    return (now.hour, now.minute) >= (hour, minute)


def _on_new_utc_day(agent_state, now):
    """Reset daily flags; auto-resume if paused. Returns True on day rollover."""
    today = now.date()
    last = agent_state.get('_scheduler_date')
    if last == today:
        return False

    if last is not None:
        agent_state['pre_session_done_today'] = False
        agent_state['post_session_done_today'] = False
        agent_state['disabled_symbols'] = set()
        agent_state['reduced_symbols'] = set()
        if agent_state.get('paused'):
            agent_state['paused'] = False
            _kv_set(agent_state, 'engine_paused', '0')
            send_alert("Trading resumed automatically at midnight.")
            logger.info("Paused cleared at UTC midnight — trading resumed")

        if now.weekday() == 0:
            agent_state['risk_manager'].correlation_cap_active = False
            agent_state['last_correlation_week'] = None
            logger.info("Correlation cap reset for new week")

    agent_state['_scheduler_date'] = today
    return True


def run_scheduled_jobs(agent_state):
    """
    Called every cycle from the execution engine.
    agent_state: a dict containing exchange, risk_manager, strategies, etc.
    """
    now = datetime.now(timezone.utc)
    _on_new_utc_day(agent_state, now)

    # Pre-session scan: once per day after 07:45 UTC (persisted in kv_state so
    # process restarts don't re-run it and burn quota / spam alerts)
    if (
        _utc_at_or_after(now, PRE_SESSION_TIME)
        and not agent_state.get('pre_session_done_today')
        and not _job_done_today(agent_state, 'pre_session', now)
    ):
        logger.info("Running pre-session scan...")
        market_data = agent_state.get('market_data', {})
        if market_data:
            response = _safe_run('pre_session_scan', pre_session_scan, market_data)
            if response:
                # ADVISORY ONLY (2026-07-09): Gemini's DISABLED/REDUCED calls
                # are logged for future scoring but no longer change position
                # sizes — an unvalidated model doesn't get authority over
                # validated strategies. Re-wire to disabled_symbols /
                # reduced_symbols only if ai_recommendations scoring shows
                # its calls actually precede bad days.
                disabled, reduced = parse_pre_session_status(response, list(market_data.keys()))
                conn = agent_state.get('conn')
                if conn is not None:
                    # Log EVERY coin's status (ACTIVE included) — scoring
                    # needs the full record, not just the warnings.
                    ts = now.isoformat()
                    for sym in market_data.keys():
                        status = ('DISABLED' if sym in disabled
                                  else 'REDUCED' if sym in reduced else 'ACTIVE')
                        conn.execute(
                            "INSERT INTO ai_recommendations (timestamp, coin, status, reason) VALUES (?,?,?,?)",
                            (ts, sym, status, 'pre-session scan'),
                        )
                    conn.commit()
                if disabled or reduced:
                    logger.info(
                        f"Gemini advisory (not applied): disabled={sorted(disabled)} reduced={sorted(reduced)}"
                    )
        agent_state['pre_session_done_today'] = True
        _mark_job_done(agent_state, 'pre_session', now)

    # Intraday drawdown check (every 15 minutes)
    global _last_intraday_check
    if now.timestamp() - _last_intraday_check > INTRADAY_CHECK_INTERVAL:
        rm = agent_state['risk_manager']
        paused = _safe_run(
            'intraday_drawdown_check',
            intraday_drawdown_check,
            rm.session_start_equity,
            rm.current_balance,
            rm.open_positions,
        )
        if paused:
            agent_state['paused'] = True
        _last_intraday_check = now.timestamp()

    # Post-session review: once per day after 21:00 UTC (kv-persisted)
    if (
        _utc_at_or_after(now, POST_SESSION_TIME)
        and not agent_state.get('post_session_done_today')
        and not _job_done_today(agent_state, 'post_session', now)
    ):
        logger.info("Running post-session review...")
        _safe_run(
            'post_session_review',
            post_session_review,
            agent_state.get('strategy_stats', {}),
            agent_state.get('trades_today', 0),
            agent_state.get('tracker_report'),
            agent_state.get('conn'),
        )
        agent_state['post_session_done_today'] = True
        _mark_job_done(agent_state, 'post_session', now)

    # Weekly correlation: Sunday >= 06:00 UTC, once per week
    maybe_weekly_correlation(agent_state, now)


def maybe_weekly_correlation(agent_state, now):
    """Run the weekly correlation check once per week and apply its verdict to
    risk_manager.correlation_cap_active (2.2: set AND clear wiring).

    Set:  Sunday >= 06:00 UTC with a usable matrix → cap follows the verdict
          (True → position cap drops to 2; False → back to normal).
    Clear: Monday 00:00 UTC in _on_new_utc_day() — the two halves together
          give the cap a defined lifecycle instead of a flag nobody toggles.
    Returns True/False when the check ran, None when it did not (wrong day/time
    or already ran this week).
    """
    week_key = now.strftime('%Y-W%W')
    if not (now.weekday() == 6
            and now.hour >= WEEKLY_CORRELATION_START_HOUR
            and agent_state.get('last_correlation_week') != week_key):
        return None
    logger.info("Running weekly correlation check...")
    matrix = agent_state.get('correlation_matrix')
    high_corr = None
    if matrix is not None and not matrix.empty:
        high_corr = _safe_run('weekly_correlation_check', weekly_correlation_check, matrix)
        rm = agent_state['risk_manager']
        rm.correlation_cap_active = bool(high_corr)
        if high_corr:
            logger.warning("Correlation cap active — max concurrent positions reduced to 2")
    # Mark the week even when the matrix was unavailable, so a missing matrix
    # cannot re-trigger the job every cycle; next week re-evaluates.
    agent_state['last_correlation_week'] = week_key
    return high_corr
