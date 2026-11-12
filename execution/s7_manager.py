"""S7 daily-momentum position flips — closes an open S7 position once the
daily direction no longer matches it, so the next entry scan can re-enter the
other way. Runs every maintenance cycle (same pattern as position_manager's
S3 trailing stops); acts only after the position has survived into a new UTC
day, so an entry is always held until at least the next day boundary.

Also enforces a soft same-day loss cap: live path losses (not ATR stops)
dominated S7 red — flat early and wait for the next flip instead of grinding.
"""

import logging
from datetime import datetime, timezone

from strategies.strategy7_daily_momentum import LONG_ONLY, SOFT_LOSS_PCT

logger = logging.getLogger('agent_execution')

S7_MODULE = 'strategy7_daily_momentum'
S7_LABEL = 'S7_Daily_Momentum'

# The keep/flip decision can only change at the UTC day boundary, so once a
# position is confirmed "keep" for a given day there's no reason to re-fetch
# daily candles every maintenance cycle (that costs ~2900 API calls/day).
_confirmed_keep = {}  # symbol -> date the current direction was confirmed


def _normalize_side(side):
    s = str(side).lower()
    return 'long' if s in ('long', 'buy') else 'short'


def _s7_qty(demo, conn, signal_id, symbol):
    """Close only this S7 fill size when BTC is shared with other strategies."""
    c = conn.cursor()
    c.execute(
        "SELECT quantity FROM orders WHERE symbol=? AND status='filled' "
        "AND timestamp >= (SELECT timestamp FROM signals WHERE id=?) "
        "ORDER BY id DESC LIMIT 1",
        (symbol, signal_id),
    )
    ord_row = c.fetchone()
    qty = float(ord_row[0]) if ord_row and ord_row[0] else 0.0
    if qty <= 0:
        positions = demo.fetch_positions([symbol])
        pos = next((p for p in positions if float(p.get('contracts', 0) or 0) != 0), None)
        if pos is None:
            return 0.0
        qty = abs(float(pos.get('contracts') or 0))
    try:
        qty = float(demo.amount_to_precision(symbol, qty))
    except Exception:
        pass
    return qty


def _close_s7(demo, conn, signal_id, symbol, side, reason):
    qty = _s7_qty(demo, conn, signal_id, symbol)
    if qty <= 0:
        return False
    current = _normalize_side(side)
    # Prefer exchange position side when DB is stale (duplicate open rows).
    try:
        positions = demo.fetch_positions([symbol])
        pos = next((p for p in positions if float(p.get('contracts', 0) or 0) != 0), None)
        if pos is not None:
            ps = str(pos.get('side') or '').lower()
            if ps in ('long', 'buy', 'short', 'sell'):
                current = _normalize_side(ps)
    except Exception:
        pass
    close_side = 'sell' if current == 'long' else 'buy'
    exit_price = None
    try:
        exit_price = float(demo.fetch_ticker(symbol)['last'])
    except Exception:
        pass
    demo.create_order(symbol, 'market', close_side, qty, None, {'reduceOnly': True})
    conn.execute(
        "UPDATE signals SET status='closed', exit_reason=?, exit_price=? WHERE id=? AND status='open'",
        (reason[:80], exit_price, signal_id),
    )
    conn.commit()
    logger.info(f"S7 exit: closed {current} {qty} {symbol} ({reason})")
    return True


def manage_s7_soft_stops(demo, public, conn):
    """Flatten S7 if adverse move from entry reaches SOFT_LOSS_PCT."""
    c = conn.cursor()
    c.execute(
        "SELECT id, symbol, side, entry FROM signals "
        "WHERE status='open' AND strategy IN (?, ?)",
        (S7_MODULE, S7_LABEL),
    )
    rows = c.fetchall()
    if not rows:
        return

    for signal_id, symbol, side, entry in rows:
        if not entry:
            continue
        try:
            # Skip DB rows that don't match the live position side (stale duplicates).
            positions = demo.fetch_positions([symbol])
            pos = next((p for p in positions if float(p.get('contracts', 0) or 0) != 0), None)
            if pos is None:
                continue
            live_side = _normalize_side(pos.get('side') or side)
            if live_side != _normalize_side(side):
                continue

            mark = float(public.fetch_ticker(symbol)['last'])
            entry_f = float(entry)
            if live_side == 'long':
                pnl_pct = (mark - entry_f) / entry_f
            else:
                pnl_pct = (entry_f - mark) / entry_f
            if pnl_pct > -SOFT_LOSS_PCT:
                continue
            _close_s7(
                demo, conn, signal_id, symbol, side,
                f"soft loss cap {pnl_pct*100:.2f}% <= -{SOFT_LOSS_PCT*100:.2f}%",
            )
        except Exception as e:
            logger.warning(f"S7 soft-stop failed for {symbol}: {e}")


def manage_s7_flips(demo, public, conn):
    manage_s7_soft_stops(demo, public, conn)

    c = conn.cursor()
    c.execute(
        "SELECT id, symbol, side, timestamp FROM signals "
        "WHERE status='open' AND strategy IN (?, ?)",
        (S7_MODULE, S7_LABEL),
    )
    rows = c.fetchall()
    if not rows:
        return

    today = datetime.now(timezone.utc).date()
    for signal_id, symbol, side, ts in rows:
        current = _normalize_side(side)
        # Long-only: flatten leftover shorts immediately (no wait for day boundary).
        if LONG_ONLY and current == 'short':
            try:
                _close_s7(demo, conn, signal_id, symbol, side, "long-only — flatten short")
            except Exception as e:
                logger.warning(f"S7 long-only short close failed for {symbol}: {e}")
            continue

        try:
            entry_date = datetime.fromisoformat(ts).date()
        except (ValueError, TypeError):
            entry_date = None
        if entry_date is not None and entry_date >= today:
            continue  # hold at least until the next UTC day boundary
        if _confirmed_keep.get(symbol) == today:
            continue

        try:
            daily = public.fetch_ohlcv(symbol, '1d', limit=3)
            y = daily[-2]
            y_ret = (y[4] - y[1]) / y[1]
        except Exception as e:
            logger.warning(f"S7 flip check failed for {symbol}: {e}")
            continue
        if y_ret == 0:
            continue

        desired = 'long' if y_ret > 0 else 'short'
        if desired == current:
            _confirmed_keep[symbol] = today
            continue
        # Long-only: on a down-day flip, close the long and stay flat (no short).
        if LONG_ONLY and desired == 'short':
            try:
                _close_s7(
                    demo, conn, signal_id, symbol, side,
                    f"flip to short blocked — flatten long (prev day {y_ret*100:+.2f}%)",
                )
            except Exception as e:
                logger.warning(f"S7 flip close failed for {symbol}: {e}")
            continue

        try:
            _close_s7(
                demo, conn, signal_id, symbol, side,
                f"flip prev day {y_ret*100:+.2f}% → {desired}",
            )
        except Exception as e:
            logger.warning(f"S7 flip close failed for {symbol}: {e}")
