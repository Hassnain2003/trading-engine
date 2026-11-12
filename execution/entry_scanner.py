"""Entry scanning: _try_entry, _scan_entries, symbol_busy, min exchange qty."""

import json
import logging
import time
from datetime import datetime, timezone

from execution import config as eng_config
from execution.config import (
    STRATEGY_TIMEFRAME,
    DEMO_EXCLUDED_SYMBOLS, MAX_ENTRIES_PER_STRATEGY_PER_DAY, ENABLE_TRADING,
)
from execution.order_manager import (
    attach_trading_stop, place_limit_order, place_s3_marketable_order,
    entry_limit_price, set_default_leverage, set_s3_leverage,
)
from execution.position_sync import normalize_side
from execution.db_manager import strategy_disabled, git_hash, config_hash
from utils.gate import check_liquidity
from trading_phase import MIN_LOT_MAX_STOP_RISK_PCT, STARTING_EQUITY, PHASE_ID

logger = logging.getLogger('agent_execution')

# 2.2: check_correlation_override() blocks a same-side signal on another
# symbol within 15 minutes. Wired but OFF pending the audit counterfactual
# (how many 34k-era signals/trades it would have blocked — report first).
CHECK_CORRELATION_OVERRIDE = False


def symbol_busy(risk_manager, conn, symbol, strategy=None):
    """One live owner per contract. Bybit one-way + shared BTC meant S9 stacked
    onto S2/S7 and a 9% stop hit the combined size."""
    if symbol in risk_manager.open_positions:
        return True
    c = conn.cursor()
    c.execute(
        "SELECT 1 FROM signals WHERE symbol=? AND status IN ('pending','open') LIMIT 1",
        (symbol,),
    )
    return c.fetchone() is not None


def ensure_min_exchange_qty(demo, symbol, qty, price, stop_loss=None,
                             min_lot_max_stop_risk_pct=MIN_LOT_MAX_STOP_RISK_PCT,
                             starting_equity=STARTING_EQUITY,
                             default_leverage=None, balance=0.0,
                             max_notional_pct=None):
    """Bump undersized lots up to Bybit min only when stop-risk fits.

    balance: current equity in USDT (used for margin and stop-risk caps).
    max_notional_pct: strategy notional cap as a fraction of equity (2.4) —
      when the min-lot notional exceeds it, a WARNING is logged so cap-bound
      bumps are visible instead of silent.
    """
    if default_leverage is None:
        default_leverage = eng_config.DEFAULT_LEVERAGE
    try:
        market = demo.market(symbol)
        min_amt = float((market.get('limits') or {}).get('amount', {}).get('min') or 0)
    except Exception:
        min_amt = 0.0
    if min_amt <= 0:
        return qty
    try:
        qty = float(demo.amount_to_precision(symbol, qty))
    except Exception:
        pass
    if qty >= min_amt:
        return qty
    notional = min_amt * float(price)
    margin = notional / float(default_leverage)
    stop_dist = None
    if stop_loss is not None:
        try:
            stop_dist = abs(float(price) - float(stop_loss))
        except (TypeError, ValueError):
            stop_dist = None
    stop_risk = (min_amt * stop_dist) if stop_dist else None
    max_stop_risk = max(balance * min_lot_max_stop_risk_pct,
                        starting_equity * min_lot_max_stop_risk_pct)
    if balance <= 0 or margin > balance * 0.4:
        logger.info(
            f"{symbol} sized {qty} < min {min_amt} "
            f"(min margin ${margin:.2f} needs ≤40% of ${balance:.2f}) — skip"
        )
        return 0.0
    if stop_risk is not None and stop_risk > max_stop_risk:
        logger.info(
            f"{symbol} sized {qty} < min {min_amt} — skip bump: "
            f"min-lot stop-risk ${stop_risk:.2f} > cap ${max_stop_risk:.2f} "
            f"({min_lot_max_stop_risk_pct*100:.0f}% of equity)"
        )
        return 0.0
    # 2.4: honest logging of actual risk vs cap on every bump.
    cap_note = ""
    if max_notional_pct is not None:
        max_notional = balance * max_notional_pct
        cap_note = f", notional cap ${max_notional:.2f} ({max_notional_pct*100:.0f}% of ${balance:.2f})"
        if notional > max_notional:
            logger.warning(
                f"{symbol} min-lot notional ${notional:.2f} EXCEEDS strategy "
                f"notional cap ${max_notional:.2f} ({max_notional_pct*100:.0f}% "
                f"of equity) — entry sized to min lot anyway; heat gate will "
                f"decide whether the entry proceeds"
            )
    logger.warning(
        f"{symbol} qty {qty} < exchange min {min_amt} — bumping to min "
        f"(notional ${notional:.2f}, margin ~${margin:.2f} @ {default_leverage}x"
        f"{f', stop-risk ${stop_risk:.2f}' if stop_risk is not None else ''}"
        f"{cap_note})"
    )
    return min_amt


def try_entry(demo, public, mod_name, func, symbol, risk_manager, conn, agent_state,
              send_alert=None, notify_trade_entry=None, add_position=None):
    """Evaluate one strategy/symbol and place an entry if conditions are met."""
    disabled = agent_state.get('disabled_symbols') or set()
    if symbol in disabled:
        logger.info(f"{symbol} skipped — Gemini DISABLED today")
        return

    # Manual-close standdown (per-strategy)
    try:
        today = datetime.now(timezone.utc).strftime('%Y-%m-%d')
        for key in (f"standdown_{mod_name}_{symbol}", f"standdown_{symbol}"):
            row = conn.execute(
                "SELECT value FROM kv_state WHERE key=?", (key,)
            ).fetchone()
            if row and row[0] == today:
                logger.info(f"{symbol} [{mod_name}] skipped — manual standdown until next UTC day")
                return
    except Exception:
        pass

    gate_ok, gate_msg = check_liquidity(public, symbol)
    if not gate_ok:
        logger.info(f"{symbol} GATE FAIL: {gate_msg}")
        return

    signal, msg = func(public, symbol)
    if signal is None:
        logger.info(f"{symbol} [{mod_name}] NO SIGNAL: {msg}")
        return

    if not risk_manager.can_open_trade():
        if risk_manager.daily_target_hit:
            tgt = risk_manager.daily_target_usdt()
            pnl = risk_manager.daily_pnl()
            logger.info(
                f"Trade blocked — daily profit target hit "
                f"({pnl:+.2f} / +{tgt:.2f} USDT); no new entries"
            )
        else:
            logger.info("Trade blocked by risk manager")
        return
    # 2.2: correlation override is wired but deliberately NOT enabled — the
    # counterfactual replay (see audit Stage 2.2 report) must decide first.
    # Set to True to activate; stays off in the meantime.
    if CHECK_CORRELATION_OVERRIDE and risk_manager.check_correlation_override(signal):
        logger.info(f"Correlation override: {symbol}")
        return
    if symbol_busy(risk_manager, conn, symbol, strategy=mod_name):
        logger.info(f"{symbol} [{mod_name}] pending/open — skip entry")
        return

    # Bybit one-way: any live contract on this symbol means another strategy owns it
    try:
        for p in demo.fetch_positions([symbol]):
            if float(p.get('contracts', 0) or 0) == 0:
                continue
            have = str(p.get('side') or '').lower() or (
                'long' if float(p.get('contracts') or 0) > 0 else 'short'
            )
            logger.info(
                f"{symbol} [{mod_name}] skipped — exchange already {have}"
            )
            return
    except Exception as e:
        logger.warning(f"{symbol} position check failed: {e}")

    strategy_key = signal.get('strategy') or mod_name

    # Portfolio heat gate moved to just before order placement (after min-lot
    # bump) so it evaluates actual stop risk (qty × stop distance), not the
    # nominal equity × dial estimate — see audit 2.3.
    base_qty = risk_manager.calculate_position_size(
        signal['price'], signal['stop_loss'], signal['side'], strategy=strategy_key,
    )
    adx = signal.get('adx', 20)
    mult = risk_manager.apply_regime_size_multiplier(adx)
    # ── M6: Correlation-aware size scaling ──
    corr_mult = risk_manager.correlation_size_multiplier(
        symbol, agent_state.get('correlation_matrix'))
    mult *= corr_mult
    if symbol in (agent_state.get('reduced_symbols') or set()):
        mult *= 0.5
    qty = base_qty * mult
    if qty <= 0:
        return

    bal = float(risk_manager.current_balance or 0)
    qty = ensure_min_exchange_qty(
        demo, symbol, qty, signal['price'],
        stop_loss=signal.get('stop_loss'),
        balance=bal,
        max_notional_pct=risk_manager.max_notional_pct_for_strategy(strategy_key),
    )
    if qty <= 0:
        return

    # 2.3: portfolio heat evaluated on ACTUAL stop risk after min-lot bump,
    # not nominal equity × dial. Pass the candidate's planned risk so open
    # positions (stored planned_risk_usd) and the new entry are compared on
    # the same footing.
    _sl = signal.get('stop_loss')
    try:
        _candidate_planned_risk = qty * abs(signal['price'] - _sl) if _sl else None
    except (TypeError, ValueError):
        _candidate_planned_risk = None
    heat_ok, heat_reason = risk_manager.portfolio_heat_ok(
        symbol, signal['side'], new_strategy=strategy_key,
        correlation_matrix=agent_state.get('correlation_matrix'),
        new_planned_risk_usd=_candidate_planned_risk,
    )
    if not heat_ok:
        logger.info(f"{symbol} [{mod_name}] skipped — {heat_reason}")
        return

    is_ioc = (mod_name == 'strategy3_vwap_bands' or signal.get('order_type') == 'ioc')
    is_s3 = (mod_name == 'strategy3_vwap_bands')
    if is_s3:
        set_s3_leverage(demo, symbol)
    else:
        set_default_leverage(demo, symbol)

    if is_ioc:
        order = place_s3_marketable_order(demo, symbol, signal['side'], qty, signal['price'])
        order_type = 'ioc_limit'
        limit_price = signal['price']
    else:
        limit_price = entry_limit_price(signal['side'], signal['price'])
        order = place_limit_order(demo, symbol, signal['side'], limit_price, qty)
        order_type = 'limit'

    if order is None:
        return

    order_id = order['id']
    risk_pct = risk_manager.risk_pct_for_strategy(strategy_key) * 100
    logger.info(
        f"📋 {'IOC' if is_ioc else 'LIMIT'}: {signal['side']} {qty:.4f} {symbol} "
        f"@ {limit_price:.4f} (risk {risk_pct:.1f}%, signal {signal['price']:.4f})"
    )

    now = datetime.now(timezone.utc).isoformat()
    c = conn.cursor()
    _sl = signal.get('stop_loss')
    _tp = signal.get('take_profit')
    _sl_attached_ok = 1 if (_sl and _tp) else 0
    try:
        if signal['side'] == 'long':
            _planned_risk = qty * (signal['price'] - _sl) if _sl else None
        else:
            _planned_risk = qty * (_sl - signal['price']) if _sl else None
    except Exception:
        _planned_risk = None
    c.execute(
        '''INSERT INTO signals (timestamp, symbol, strategy, side, entry, stop_loss, take_profit, status, phase_id, expected_price,
           order_id, planned_risk_usd, sl_attached_ok, size_multipliers_json, git_hash, config_hash)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
        (now, symbol, mod_name, signal['side'], signal['price'],
         _sl, _tp, 'pending', PHASE_ID, limit_price,
         str(order_id), _planned_risk, _sl_attached_ok,
         json.dumps({
             'base_qty': base_qty, 'regime_mult': mult,
             'corr_mult': corr_mult, 'final_qty': qty,
         }),
         git_hash(), config_hash(risk_manager, eng_config)),
    )
    c.execute(
        '''INSERT INTO orders (timestamp, symbol, side, order_type, price, quantity, order_id, status)
           VALUES (?,?,?,?,?,?,?,?)''',
        (now, symbol, signal['side'], order_type, limit_price, qty, str(order_id), 'open'),
    )
    conn.commit()


def scan_entries(demo, public, strategies, strategy_timeframe, timeframes, risk_manager,
                 conn, agent_state, send_alert=None, notify_trade_entry=None,
                 add_position=None):
    """Run entry scan for strategies matching the given timeframes."""
    if agent_state.get('paused'):
        logger.info("Paused — skipping entry scan")
        return

    # ── M5: Trade frequency cap ──
    # Track entries per strategy per UTC day; reset on day rollover.
    today = datetime.now(timezone.utc).strftime('%Y-%m-%d')
    if agent_state.get('_entry_cap_date') != today:
        agent_state['_entry_cap_date'] = today
        agent_state['_entries_per_strategy'] = {}
    entries_per_strategy = agent_state.setdefault('_entries_per_strategy', {})

    logger.info(f"=== Entry scan ({','.join(sorted(timeframes))}) ===")
    # ── Safe default: never place a new order unless explicitly opted in ──
    if not ENABLE_TRADING:
        logger.info(
            "Trading disabled — set ENABLE_TRADING=true to place new orders. "
            "Entry scan skipped (no orders will be placed)."
        )
        return
    for mod_name, func, coins in strategies:
        if strategy_disabled(conn, mod_name):
            logger.info(f"{mod_name} disabled via kill switch — skipping")
            continue
        # Enforce per-strategy daily entry cap
        n_entries = entries_per_strategy.get(mod_name, 0)
        if n_entries >= MAX_ENTRIES_PER_STRATEGY_PER_DAY:
            logger.info(
                f"{mod_name} daily entry cap reached ({n_entries}/{MAX_ENTRIES_PER_STRATEGY_PER_DAY}) — skipping")
            continue
        tf = strategy_timeframe.get(mod_name)
        if tf not in timeframes:
            continue
        for symbol in coins:
            # Re-check cap per symbol in case a previous symbol in this loop hit it
            n_entries = entries_per_strategy.get(mod_name, 0)
            if n_entries >= MAX_ENTRIES_PER_STRATEGY_PER_DAY:
                break
            try_entry(demo, public, mod_name, func, symbol, risk_manager, conn, agent_state,
                      send_alert=send_alert, notify_trade_entry=notify_trade_entry,
                      add_position=add_position)
            # Increment only if try_entry actually placed an order
            n_entries = entries_per_strategy.get(mod_name, 0)
            if n_entries < MAX_ENTRIES_PER_STRATEGY_PER_DAY:
                entries_per_strategy[mod_name] = n_entries + 1
            time.sleep(0.2)


# ── M8: Exit beyond SL/TP — signal reversal ──────────────────────────────────
# Close an open position when the strategy's current signal flips direction.
# This captures regime changes without waiting for SL/TP.
def check_signal_reversal_exits(demo, public, strategies, risk_manager, conn,
                                agent_state, send_alert=None):
    """Close open positions when the live signal reverses direction.

    For each open position, re-runs the strategy's signal function. If the
    new signal side opposes the position side, closes the position via the
    exchange and marks the signal closed in the DB.

    Returns the number of positions closed.
    """
    closed_count = 0
    # Build symbol -> (mod_name, func) map
    symbol_to_strategy = {}
    for mod_name, func, coins in strategies:
        for symbol in coins:
            symbol_to_strategy[symbol] = (mod_name, func)

    for symbol, pos in list(risk_manager.open_positions.items()):
        if symbol not in symbol_to_strategy:
            continue
        mod_name, func = symbol_to_strategy[symbol]
        pos_side = pos.get('side', 'long')
        try:
            signal, _msg = func(public, symbol)
        except Exception as e:
            logger.debug(f"Signal reversal check failed for {symbol}: {e}")
            continue
        if signal is None:
            continue
        sig_side = signal.get('side', 'long')
        # Same direction — hold
        if sig_side == pos_side:
            continue
        # Reversed — close the position
        entry = pos.get('entry', 0)
        qty = pos.get('quantity', 0)
        strategy = pos.get('strategy', mod_name)
        try:
            from utils.manual_close import close_positions
            results = close_positions(demo, conn, coins=[symbol])
            ok = [r for r in results if r.get('ok')]
            if ok:
                risk_manager.remove_position(symbol)
                c = conn.cursor()
                c.execute(
                    "UPDATE signals SET status='closed', exit_reason='signal_reversal', "
                    "exit_price=? WHERE symbol=? AND status='open'",
                    (ok[0].get('exit_price'), symbol),
                )
                conn.commit()
                closed_count += 1
                msg = (
                    f'🔄 SIGNAL REVERSAL: {symbol} {pos_side}→{sig_side} closed. '
                    f'Strategy: {strategy}'
                )
                logger.warning(msg)
                if send_alert:
                    send_alert(msg)
        except Exception as e:
            logger.warning(f"Signal reversal close failed for {symbol}: {e}")

    return closed_count
