"""Live position reconciliation, SL/TP helpers, and P&L utilities."""

import logging
import time
from datetime import datetime, timezone

from execution.config import MAX_POSITION_AGE_SEC
from trading_phase import phase_equity

logger = logging.getLogger('agent_execution')


def normalize_side(side):
    s = str(side).lower()
    if s in ('long', 'buy'):
        return 'long'
    return 'short'


def pnl_usdt(side, entry, exit_price, qty):
    side = normalize_side(side)
    if not entry or not exit_price or not qty:
        return None
    if side == 'long':
        return (exit_price - entry) * qty
    return (entry - exit_price) * qty


def exit_price(demo, public, symbol):
    """Prefer the actual closing execution price; fall back to ticker."""
    try:
        for t in reversed(demo.fetch_my_trades(symbol, limit=10)):
            if float(t.get('info', {}).get('closedSize') or 0) > 0:
                return float(t['price'])
    except Exception:
        pass
    try:
        return float(public.fetch_ticker(symbol)['last'])
    except Exception:
        return None


def filled_qty(conn, symbol):
    c = conn.cursor()
    c.execute(
        """SELECT quantity FROM orders
           WHERE symbol=? AND status='filled' ORDER BY id DESC LIMIT 1""",
        (symbol,),
    )
    row = c.fetchone()
    return float(row[0]) if row else None


def close_and_alert(c, conn, demo, symbol, side, entry, exit_price, qty,
                    signal_id=None, strategy=None, send_alert=None):
    """Mark a signal closed and send an ntfy exit alert."""
    if signal_id is not None:
        exit_ts = datetime.now(timezone.utc).isoformat()
        c.execute(
            """UPDATE signals SET status='closed', exit_reason='exchange_closed',
               exit_price=?, exit_fill_price=?, exit_ts=? WHERE id=?""",
            (exit_price, exit_price, exit_ts, signal_id),
        )
        conn.commit()
        # Populate fee/funding/P&L columns from the ledger when available.
        try:
            exit_fee = c.execute(
                "SELECT SUM(fee) FROM exchange_fills WHERE order_id=(SELECT order_id FROM signals WHERE id=?) AND fee IS NOT NULL",
                (signal_id,),
            ).fetchone()
            funding = c.execute(
                "SELECT SUM(amount) FROM funding_payments WHERE symbol=? AND timestamp >= (SELECT entry_ts FROM signals WHERE id=?)",
                (symbol, signal_id),
            ).fetchone()
            gross = pnl_usdt(side, float(entry), exit_price, qty or 0)
            entry_fee = c.execute(
                "SELECT entry_fee FROM signals WHERE id=?", (signal_id,)
            ).fetchone()
            exit_fee_val = exit_fee[0] if exit_fee and exit_fee[0] is not None else 0.0
            entry_fee_val = entry_fee[0] if entry_fee and entry_fee[0] is not None else 0.0
            funding_val = funding[0] if funding and funding[0] is not None else 0.0
            net = None
            if gross is not None:
                net = gross - entry_fee_val - exit_fee_val + funding_val
            c.execute(
                """UPDATE signals SET exit_fee=?, funding_total=?, net_pnl_usd=?
                   WHERE id=?""",
                (exit_fee_val, funding_val, net, signal_id),
            )
            conn.commit()
        except Exception:
            pass
    if exit_price is not None and entry is not None:
        pnl = pnl_usdt(side, float(entry), exit_price, qty or 0)
        pnl_str = f"{pnl:+.2f}" if pnl is not None else "n/a"
        strat = f" | Strategy: {strategy}" if strategy else ""
        msg = (
            f"Trade Closed: {symbol} {normalize_side(side)} | Entry {float(entry):.4f} | "
            f"Exit {exit_price:.4f} | P&L {pnl_str} USDT{strat}"
        )
        if send_alert:
            send_alert(msg)
        logger.info(f"Exit alert sent: {symbol} P&L {pnl_str} USDT")
    logger.info(f"Reconciled closed position {symbol}")


def reconcile_live_positions(demo, public, conn, risk_manager, agent_state,
                              send_alert=None, check_weekly_kill_switch=None):
    """Sync risk_manager + DB with exchange when PM is disabled (native SL/TP exits).

    Handles:
      - Re-syncing open positions into risk_manager on restart
      - Guaranteeing exchange-native SL/TP on re-synced positions
      - Phantom-close guard (2 consecutive missing cycles)
      - Weekly kill switch check on balance sync
    """
    try:
        positions = [
            p for p in demo.fetch_positions()
            if float(p.get('contracts', 0) or 0) != 0
        ]
    except Exception as e:
        logger.warning(f"Live position sync failed: {e}")
        return

    live = {p['symbol'] for p in positions}
    prev_live = agent_state.get('prev_live_symbols') or set()
    prev_snapshot = dict(agent_state.get('live_snapshot', {}))
    snapshot = {}
    for p in positions:
        symbol = p['symbol']
        snapshot[symbol] = {
            'side': p.get('side'),
            'entry': float(p.get('entryPrice') or p.get('average') or 0),
            'qty': float(p.get('contracts', 0) or 0),
        }

    upnl = sum(float(p.get('unrealizedPnl') or 0) for p in positions)
    try:
        balance = demo.fetch_balance()
        usdt = balance.get('USDT', {}).get('total')
        if usdt is not None:
            usdt = float(usdt)
            from trading_phase import set_wallet_baseline, get_wallet_baseline
            # _ensure_wallet_baseline equivalent inline
            from trading_phase import FORCE_EQUITY
            if FORCE_EQUITY:
                if get_wallet_baseline() is not None:
                    pass
                else:
                    row = conn.execute(
                        "SELECT value FROM kv_state WHERE key='wallet_baseline'"
                    ).fetchone()
                    if row and row[0]:
                        set_wallet_baseline(float(row[0]))
                    else:
                        set_wallet_baseline(usdt)
                        conn.execute(
                            "INSERT OR REPLACE INTO kv_state (key, value) VALUES (?, ?)",
                            ("wallet_baseline", str(float(usdt))),
                        )
                        conn.commit()
                        logger.info(f"Phase wallet baseline locked at ${float(usdt):,.2f}")

            action = risk_manager.update_balance(usdt, unrealized=upnl)
            if action == 'flatten':
                logger.warning("Daily −4% hit — flattening all positions.")
                from execution.order_manager import flatten_for_daily_loss
                flatten_for_daily_loss(demo, conn, risk_manager, send_alert=send_alert)
            elif action == 'lock':
                from execution.order_manager import alert_daily_lock_once
                alert_daily_lock_once(risk_manager, send_alert=send_alert)

            eq = phase_equity(usdt)
            conn.execute(
                "INSERT INTO equity_snapshots (timestamp, balance, unrealized, equity) VALUES (?,?,?,?)",
                (datetime.now(timezone.utc).isoformat(), eq, upnl, eq + upnl),
            )
            conn.commit()

            # Weekly kill switch
            if check_weekly_kill_switch:
                try:
                    killed = check_weekly_kill_switch(eq + upnl)
                    if killed and not getattr(agent_state.get('_engine_ref', object()), '_weekly_kill_alerted', False):
                        logger.warning("Weekly kill switch armed — new entries paused until next UTC week.")
                except Exception as e:
                    logger.warning(f"Weekly kill switch check failed: {e}")
    except Exception as e:
        logger.warning(f"Balance sync failed: {e}")

    handled = set()
    c = conn.cursor()

    # Re-sync open positions into risk_manager on restart
    tracked = set(risk_manager.open_positions.keys())
    for symbol in live - tracked:
        c.execute(
            """SELECT strategy, side, entry, stop_loss, take_profit FROM signals
               WHERE symbol=? AND status='open' ORDER BY id DESC LIMIT 1""",
            (symbol,),
        )
        row = c.fetchone()
        if not row:
            continue
        strategy, side, entry, stop, target = row
        c.execute(
            """SELECT order_id FROM orders WHERE symbol=? AND status='filled'
               ORDER BY id DESC LIMIT 1""",
            (symbol,),
        )
        order_row = c.fetchone()
        order_id = order_row[0] if order_row else None
        qty = snapshot.get(symbol, {}).get('qty')
        # 2.3: store actual stop risk (qty × stop distance) so portfolio heat
        # sums stored risk instead of nominal dial.
        try:
            _qty = float(qty or 0)
            _entry = float(entry) if entry is not None else None
            _stop = float(stop) if stop is not None else None
            _planned_risk_usd = _qty * abs(_entry - _stop) if _qty > 0 and _entry is not None and _stop is not None else None
        except (TypeError, ValueError):
            _planned_risk_usd = None
        risk_manager.add_position(symbol, side, entry, stop, target, qty, order_id,
                                  strategy=strategy, planned_risk_usd=_planned_risk_usd)
        logger.info(f"Re-synced live position on startup: {side} {symbol} qty={qty}")

        # Guarantee exchange-native SL/TP on re-synced positions
        if stop and target:
            try:
                from execution.order_manager import attach_trading_stop
                attach_trading_stop(demo, symbol, stop, target, side=side)
            except Exception as e:
                logger.warning(f"SL/TP re-attach failed for {symbol}: {e}")

    # Phantom-close guard: 2 consecutive missing cycles
    miss_counts = agent_state.get('_position_miss_counts', {})
    for symbol in list(miss_counts):
        if symbol in live:
            del miss_counts[symbol]

    def confirmed_missing(symbol):
        n = miss_counts.get(symbol, 0) + 1
        miss_counts[symbol] = n
        if n < 2:
            logger.info(f"{symbol} missing from exchange (1st time) — awaiting confirmation")
            return False
        return True

    for symbol in tracked - live:
        if not confirmed_missing(symbol):
            continue
        risk_manager.remove_position(symbol)
        c.execute(
            """SELECT id, strategy, side, entry FROM signals
               WHERE symbol=? AND status='open' ORDER BY id DESC LIMIT 1""",
            (symbol,),
        )
        row = c.fetchone()
        if row:
            signal_id, strategy, side, entry = row
            qty = filled_qty(conn, symbol) or snapshot.get(symbol, {}).get('qty')
            close_and_alert(c, conn, demo, symbol, side, entry,
                            exit_price(demo, public, symbol), qty, signal_id, strategy, send_alert)

    for signal_id, symbol, strategy, side, entry in c.execute(
        "SELECT id, symbol, strategy, side, entry FROM signals WHERE status='open'"
    ).fetchall():
        if symbol in live or symbol in handled:
            continue
        if not confirmed_missing(symbol):
            continue
        risk_manager.remove_position(symbol)
        qty = filled_qty(conn, symbol)
        close_and_alert(c, conn, demo, symbol, side, entry,
                        exit_price(demo, public, symbol), qty, signal_id, strategy, send_alert)

    for symbol in prev_live - live:
        if symbol in handled or symbol in tracked:
            continue
        prev_meta = prev_snapshot.get(symbol, {})
        entry = prev_meta.get('entry')
        side = prev_meta.get('side') or 'long'
        qty = prev_meta.get('qty')
        xprice = exit_price(demo, public, symbol)
        if xprice is not None and entry:
            close_and_alert(c, conn, demo, symbol, side, entry, xprice, qty, None, None, send_alert)
            logger.info(f"Reconciled external closed position {symbol}")
        handled.add(symbol)

    agent_state['live_snapshot'] = snapshot
    agent_state['prev_live_symbols'] = live
    agent_state['_position_miss_counts'] = miss_counts


# ── M3: Max position age / time exit ─────────────────────────────────────────
def check_max_position_age(demo, conn, risk_manager, agent_state,
                           send_alert=None, max_age_sec=None):
    """Close any open position held longer than max_age_sec.

    Checks risk_manager.open_positions (which carries entry_time from add_position).
    Sends an ntfy alert per closed position. Returns the number closed.
    """
    if max_age_sec is None:
        max_age_sec = MAX_POSITION_AGE_SEC

    closed_count = 0
    now = datetime.now(timezone.utc)

    for symbol, pos in list(risk_manager.open_positions.items()):
        entry_time = pos.get('entry_time')
        if entry_time is None:
            continue
        age = (now - entry_time).total_seconds()
        if age > max_age_sec:
            side = pos.get('side', 'long')
            entry = pos.get('entry', 0)
            qty = pos.get('quantity', 0)
            strategy = pos.get('strategy', 'unknown')
            # Close on exchange
            try:
                from utils.manual_close import close_positions
                results = close_positions(demo, conn, coins=[symbol])
                ok = [r for r in results if r.get('ok')]
                if ok:
                    risk_manager.remove_position(symbol)
                    # Mark signal closed
                    c = conn.cursor()
                    c.execute(
                        "UPDATE signals SET status='closed', exit_reason='max_position_age', "
                        "exit_price=? WHERE symbol=? AND status='open'",
                        (ok[0].get('exit_price'), symbol),
                    )
                    conn.commit()
                    closed_count += 1
                    msg = (
                        f'⏰ MAX POSITION AGE: {symbol} {side} closed after '
                        f'{int(age / 3600)}h (limit {int(max_age_sec / 3600)}h). '
                        f'Strategy: {strategy}'
                    )
                    logger.warning(msg)
                    if send_alert:
                        send_alert(msg)
            except Exception as e:
                logger.warning(f'Max-age close failed for {symbol}: {e}')

    return closed_count
