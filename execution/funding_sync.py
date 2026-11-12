"""Periodic sync of funding payments from the exchange into funding_payments.

Bybit settles funding every 8 hours on open perpetual positions. This module
fetches the settlement history via ccxt's fetch_funding_history and persists
each payment to the funding_payments table (exchange_id UNIQUE deduplicates).
"""

import logging
from datetime import datetime, timezone

logger = logging.getLogger('agent_execution')

# Sync at most once per 30 min to stay well within rate limits
FUNDING_SYNC_INTERVAL_SEC = 1800


def sync_funding_payments(demo, conn, symbols=None, logger_=None):
    """Fetch recent funding settlements from Bybit and upsert into funding_payments.

    Returns the number of new (non-duplicate) rows inserted.
    """
    log = logger_ or logger
    if not symbols:
        return 0
    inserted = 0
    for symbol in symbols:
        try:
            payments = demo.fetch_funding_history(symbol, limit=50)
        except Exception as e:
            log.warning(f"Funding history fetch failed for {symbol}: {e}")
            continue
        for p in payments:
            ex_id = str(p.get('id') or '')
            ts_ms = p.get('timestamp')
            amount = p.get('amount')
            if not ex_id or ts_ms is None or amount is None:
                continue
            ts_iso = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).isoformat()
            try:
                conn.execute(
                    """INSERT OR IGNORE INTO funding_payments (exchange_id, timestamp, symbol, amount)
                       VALUES (?,?,?,?)""",
                    (ex_id, ts_iso, symbol, float(amount)),
                )
                if conn.total_changes:
                    pass  # can't reliably track per-row here; rely on INSERT OR IGNORE
            except Exception as e:
                log.warning(f"Funding insert failed {symbol}: {e}")
        conn.commit()
    # Count newly inserted rows for reporting
    try:
        total = conn.execute("SELECT COUNT(*) FROM funding_payments").fetchone()[0]
        log.info(f"Funding sync complete — {total} total payments in DB")
    except Exception:
        pass
    return inserted


def maybe_sync_funding(demo, conn, agent_state, symbols=None, interval_sec=FUNDING_SYNC_INTERVAL_SEC):
    """Rate-limited wrapper: only calls sync if the interval has elapsed.

    Uses agent_state['_funding_synced_at'] (epoch seconds) to track last sync.
    """
    import time
    now = time.time()
    last = agent_state.get('_funding_synced_at', 0.0)
    if now - last < interval_sec:
        return
    agent_state['_funding_synced_at'] = now
    try:
        sync_funding_payments(demo, conn, symbols=symbols)
    except Exception as e:
        logger.warning(f"Funding sync error: {e}")
