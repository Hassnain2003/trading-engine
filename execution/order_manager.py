"""Order placement, SL/TP attachment, pending-order reconciliation, and daily-loss flatten."""

import logging
import time
from datetime import datetime, timezone

from execution.config import (
    DEFAULT_LEVERAGE, S3_LEVERAGE, LIMIT_OFFSET_PCT, PENDING_ORDER_TTL_SEC,
    IOC_MAX_CROSS_PCT,
)
from execution.position_sync import normalize_side
from trading_phase import MIN_LOT_MAX_STOP_RISK_PCT, STARTING_EQUITY

logger = logging.getLogger('agent_execution')


def place_limit_order(demo, symbol, side, price, quantity):
    """Place a post-only limit order. SL/TP is attached after fill."""
    try:
        price = float(demo.price_to_precision(symbol, price))
        qty = float(demo.amount_to_precision(symbol, quantity))
        params = {'postOnly': True}
        if side == 'long':
            return demo.create_limit_buy_order(symbol, qty, price, params)
        return demo.create_limit_sell_order(symbol, qty, price, params)
    except Exception as e:
        logger.error(f"Order error: {e}")
        return None

def place_s3_marketable_order(demo, symbol, side, quantity, entry_price):
    """IOC limit priced by walking the order book — fill the whole size."""
    try:
        ob = demo.fetch_order_book(symbol, limit=25)
        levels = ob['asks'] if side == 'long' else ob['bids']
        if not levels or levels[0][0] <= 0:
            price = entry_price
        else:
            best = levels[0][0]
            price = best
            cum = 0.0
            for lvl_price, lvl_size in levels:
                cum += lvl_size
                price = lvl_price
                if cum >= quantity:
                    break
            if side == 'long':
                price = min(price, best * (1 + IOC_MAX_CROSS_PCT))
            else:
                price = max(price, best * (1 - IOC_MAX_CROSS_PCT))

        if entry_price and abs(price / entry_price - 1) > 0.02:
            logger.error(
                f"Refusing {symbol} IOC: book price {price} deviates "
                f">{2}% from signal price {entry_price} — broken demo book?"
            )
            return None

        price = float(demo.price_to_precision(symbol, price))
        qty = float(demo.amount_to_precision(symbol, quantity))
        params = {'timeInForce': 'IOC'}

        if side == 'long':
            return demo.create_limit_buy_order(symbol, qty, price, params)
        return demo.create_limit_sell_order(symbol, qty, price, params)
    except Exception as e:
        logger.error(f"S3 marketable order error: {e}")
        return None


def entry_limit_price(side, signal_price, limit_offset_pct=LIMIT_OFFSET_PCT):
    """Limit price offset from the signal price (post-only, passive entry)."""
    if side == 'long':
        return signal_price * (1 - limit_offset_pct)
    return signal_price * (1 + limit_offset_pct)
def set_s3_leverage(demo, symbol, s3_leverage=S3_LEVERAGE):
    """Set isolated margin + S3 leverage for S3 VWAP band trades."""
    try:
        demo.set_margin_mode('isolated', symbol)
    except Exception as e:
        logger.debug(f"S3 isolated margin skipped {symbol}: {e}")
    try:
        demo.set_leverage(s3_leverage, symbol)
        logger.info(f"S3 leverage {s3_leverage}x isolated → {symbol}")
    except Exception as e:
        err = str(e)
        if '110043' in err or 'leverage not modified' in err.lower():
            logger.info(f"S3 leverage already {s3_leverage}x on {symbol}")
        else:
            logger.warning(f"S3 leverage set failed for {symbol}: {e}")


def set_default_leverage(demo, symbol, default_leverage=DEFAULT_LEVERAGE):
    """Reset a symbol to cross/DEFAULT_LEVERAGE before a non-S3 entry."""
    try:
        demo.set_margin_mode('cross', symbol)
    except Exception as e:
        logger.debug(f"Cross margin reset skipped {symbol}: {e}")
    try:
        demo.set_leverage(default_leverage, symbol)
        logger.info(f"Leverage {default_leverage}x cross → {symbol}")
    except Exception as e:
        err = str(e)
        if '110043' in err or 'leverage not modified' in err.lower():
            logger.info(f"Leverage already {default_leverage}x on {symbol}")
        else:
            logger.warning(f"Leverage reset failed for {symbol}: {e}")


def existing_exchange_stop(demo, symbol):
    """Return (side, stop_loss) for a live contract, or (None, None)."""
    try:
        for p in demo.fetch_positions([symbol]):
            if float(p.get('contracts', 0) or 0) == 0:
                continue
            if p.get('symbol') != symbol:
                continue
            info = p.get('info') or {}
            raw = str(info.get('stopLoss') or p.get('stopLossPrice') or '').strip()
            sl = None
            if raw and raw not in ('0', '0.0'):
                try:
                    sl = float(raw)
                except (TypeError, ValueError):
                    sl = None
            side = str(p.get('side') or '').lower()
            if side not in ('long', 'short'):
                side = 'long' if float(p.get('contracts') or 0) > 0 else 'short'
            return side, sl
    except Exception as e:
        logger.warning(f"Existing stop lookup failed for {symbol}: {e}")
    return None, None


def attach_trading_stop(demo, symbol, sl, tp, side=None, attempts=2):
    """Attach SL/TP after fill and verify the exchange actually shows a stop.

    Never widen an existing SL on a shared contract (S9's 9% must not
    replace S2's ~3% on leftover BTC).
    """
    if sl is None or tp is None:
        return False
    have_side, existing_sl = existing_exchange_stop(demo, symbol)
    pos_side = normalize_side(side or have_side or 'long')
    if existing_sl is not None:
        proposed = float(sl)
        tighter = (
            existing_sl > proposed if pos_side == 'long' else existing_sl < proposed
        )
        if tighter:
            logger.info(
                f"Keeping tighter existing SL {existing_sl} on {symbol} "
                f"(proposed {proposed}, {pos_side}) — not widening"
            )
            return True
    market_id = demo.market(symbol)['id']
    last_err = None
    for attempt in range(1, attempts + 1):
        try:
            demo.privatePostV5PositionTradingStop({
                'category': 'linear',
                'symbol': market_id,
                'positionIdx': 0,
                'tpslMode': 'Full',
                'slOrderType': 'Market',
                'stopLoss': str(sl),
                'takeProfit': str(tp),
                'slTriggerBy': 'LastPrice',
                'tpTriggerBy': 'LastPrice',
            })
        except Exception as e:
            last_err = e
            logger.warning(f"SL/TP attach attempt {attempt} failed for {symbol}: {e}")
            time.sleep(0.4)
            continue

        try:
            for p in demo.fetch_positions([symbol]):
                if float(p.get('contracts', 0) or 0) == 0:
                    continue
                info = p.get('info') or {}
                attached = str(info.get('stopLoss') or p.get('stopLossPrice') or '').strip()
                if attached and attached not in ('0', '0.0'):
                    logger.info(f"SL/TP attached+verified for {symbol} (SL={attached})")
                    return True
                logger.warning(
                    f"SL/TP attach attempt {attempt} for {symbol} returned OK "
                    f"but position shows stopLoss={attached!r} — retrying"
                )
        except Exception as e:
            last_err = e
            logger.warning(f"SL/TP verify failed for {symbol}: {e}")
        time.sleep(0.4)

    logger.error(f"SL/TP NOT verified for {symbol} after {attempts} attempts (last={last_err})")
    return False

def reconcile_pending_orders(demo, conn, risk_manager, pending_order_ttl_sec=600,
                              notify_trade_entry=None, add_position=None):
    """Reconcile 'pending' signals against the exchange: attach SL/TP on fill,
    cancel stale unfilled orders, clear orphans."""
    c = conn.cursor()
    c.execute(
        """SELECT id, symbol, strategy, side, entry, stop_loss, take_profit, timestamp, expected_price
           FROM signals WHERE status='pending' ORDER BY id"""
    )
    rows = c.fetchall()
    if not rows:
        return

    try:
        open_pos = {
            p['symbol'] for p in demo.fetch_positions()
            if float(p.get('contracts', 0) or 0) != 0
        }
    except Exception as e:
        logger.warning(f"fetch_positions failed, skipping reconcile: {e}")
        return

    open_order_ids = set()
    try:
        for order in demo.fetch_open_orders():
            open_order_ids.add(str(order['id']))
    except Exception as e:
        logger.warning(f"fetch_open_orders failed: {e}")

    for signal_id, symbol, strategy, side, entry, sl, tp, ts, expected_price in rows:
        c.execute(
            "SELECT order_id, quantity FROM orders WHERE symbol=? AND status='open' "
            "ORDER BY id DESC LIMIT 1",
            (symbol,),
        )
        order_row = c.fetchone()
        placed_at = datetime.fromisoformat(ts)
        if placed_at.tzinfo is None:
            placed_at = placed_at.replace(tzinfo=timezone.utc)
        age = (datetime.now(timezone.utc) - placed_at).total_seconds()

        if not order_row:
            if age >= pending_order_ttl_sec:
                c.execute(
                    "UPDATE signals SET status='cancelled', exit_reason='stale_pending_no_order' WHERE id=?",
                    (signal_id,),
                )
                conn.commit()
                logger.info(f"Cleared orphan pending {symbol} #{signal_id} after {int(age)}s")
            continue
        order_id, qty = order_row
        oid = str(order_id)

        filled = symbol in open_pos
        still_on_book = oid in open_order_ids

        if filled:
            attach_trading_stop(demo, symbol, sl, tp, side=side)
            fill_ts = datetime.now(timezone.utc).isoformat()
            fill_entry = entry
            fill_qty = float(qty or 0)
            try:
                for p in demo.fetch_positions():
                    if p['symbol'] == symbol and float(p.get('contracts', 0) or 0) != 0:
                        fill_entry = float(p.get('entryPrice') or p.get('average') or entry)
                        ex_qty = abs(float(p.get('contracts') or 0))
                        if fill_qty > 0 and ex_qty > 0:
                            fill_qty = min(fill_qty, ex_qty)
                        elif ex_qty > 0 and fill_qty <= 0:
                            fill_qty = ex_qty
                        break
            except Exception:
                pass
            c.execute(
                """UPDATE signals SET status='open', timestamp=?, entry=?,
                   entry_ts=?, entry_fill_price=?, entry_qty=?,
                   order_id=? WHERE id=?""",
                (fill_ts, fill_entry, fill_ts, fill_entry, fill_qty,
                 order_id, signal_id),
            )
            c.execute(
                "UPDATE orders SET status='filled', quantity=? WHERE order_id=? AND status='open'",
                (fill_qty, oid),
            )
            conn.commit()
            # Record actual fill + slippage on the signal row
            try:
                if expected_price and fill_entry and expected_price > 0:
                    if side == 'long':
                        slip_bps = (fill_entry - expected_price) / expected_price * 10000
                    else:
                        slip_bps = (expected_price - fill_entry) / expected_price * 10000
                    c.execute(
                        "UPDATE signals SET actual_fill_price=?, slippage_bps=? WHERE id=?",
                        (fill_entry, slip_bps, signal_id),
                    )
                    conn.commit()
            except Exception:
                pass
            # Pull entry fee from the exchange-fill ledger if synced.
            try:
                c.execute(
                    "SELECT SUM(fee) FROM exchange_fills WHERE order_id=? AND fee IS NOT NULL",
                    (order_id,),
                )
                _fee_row = c.fetchone()
                if _fee_row and _fee_row[0] is not None:
                    c.execute(
                        "UPDATE signals SET entry_fee=? WHERE id=? AND entry_fee IS NULL",
                        (_fee_row[0], signal_id),
                    )
                    conn.commit()
            except Exception:
                pass
            if add_position and symbol not in risk_manager.open_positions:
                # 2.3: record actual stop risk (qty × stop distance) so
                # portfolio_heat_ok sums stored risk instead of nominal dial.
                try:
                    _sl = float(sl)
                    _planned_risk_usd = fill_qty * abs(fill_entry - _sl) if fill_entry else None
                except (TypeError, ValueError):
                    _planned_risk_usd = None
                add_position(symbol, side, fill_entry, sl, tp, fill_qty, order_id,
                             strategy=strategy, planned_risk_usd=_planned_risk_usd)
            if notify_trade_entry:
                notify_trade_entry(symbol, side, fill_qty, fill_entry, sl, tp, strategy)
            logger.info(f"✅ Filled: {side} {fill_qty} {symbol} @ {fill_entry}")
            continue

        if still_on_book:
            if age >= pending_order_ttl_sec:
                try:
                    demo.cancel_order(order_id, symbol)
                except Exception as e:
                    logger.warning(f"Cancel failed {symbol}: {e}")
                c.execute(
                    "UPDATE signals SET status='cancelled', exit_reason='unfilled' WHERE id=?",
                    (signal_id,),
                )
                c.execute(
                    "UPDATE orders SET status='cancelled' WHERE order_id=? AND status='open'",
                    (oid,),
                )
                conn.commit()
                logger.info(f"Cancelled unfilled limit {symbol} after {int(age)}s")
            continue

        c.execute(
            "UPDATE signals SET status='cancelled', exit_reason='not_on_book' WHERE id=?",
            (signal_id,),
        )
        c.execute(
            "UPDATE orders SET status='cancelled' WHERE order_id=? AND status='open'",
            (oid,),
        )
        conn.commit()
        logger.info(f"Cleared stale pending {symbol} (order left book, no fill)")


def flatten_for_daily_loss(demo, conn, risk_manager, send_alert=None):
    """Market-close everything once daily mark-to-market hits −4%."""
    if risk_manager.flattened_for_daily_loss:
        return
    from utils.manual_close import close_positions
    try:
        results = close_positions(demo, conn, coins=None)
    except Exception as e:
        logger.error(f"Daily-loss flatten failed: {e}")
        if send_alert:
            send_alert(f"Daily loss halt: flatten FAILED ({e})")
        return
    risk_manager.flattened_for_daily_loss = True
    ok = [r for r in results if r.get('ok')]
    bad = [r for r in results if not r.get('ok')]
    for r in ok:
        symbol = r['symbol']
        exit_price = None
        try:
            exit_price = float(demo.fetch_ticker(symbol)['last'])
        except Exception:
            pass
        conn.execute(
            "UPDATE signals SET status='closed', exit_reason='daily_loss_flatten', "
            "exit_price=? WHERE symbol=? AND status='open'",
            (exit_price, symbol),
        )
        risk_manager.remove_position(symbol)
    conn.commit()
    pnl = risk_manager.daily_pnl()
    msg = (
        f"Daily loss halt: flattened {len(ok)} position(s) "
        f"(P&L {pnl:+.2f} USDT). No new entries for 24h."
    )
    if bad:
        msg += f" {len(bad)} close(s) failed."
    logger.warning(msg)
    if send_alert:
        send_alert(msg)


def alert_daily_lock_once(risk_manager, send_alert=None):
    """−2% session loss: entries are already paused; send ntfy once per UTC day."""
    if risk_manager.daily_lock_alerted:
        return
    risk_manager.daily_lock_alerted = True
    pnl = risk_manager.daily_pnl()
    msg = (
        f"Daily loss lock (−2%): session P&L {pnl:+.2f} USDT. "
        f"New entries paused for the rest of the UTC day; "
        f"open positions still run to SL/TP."
    )
    logger.warning(msg)
    if send_alert:
        send_alert(msg)
