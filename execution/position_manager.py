"""S3-only trailing stop updates — no entries, no TP changes."""

import logging
import os

import pandas as pd

try:
    import pandas_ta as ta
except ImportError:  # pandas_ta/numba unavailable on this Python (e.g. 3.14); the real lib is required for trailing-stop math and is available on CI (Python 3.12).
    ta = None

logger = logging.getLogger('agent_execution')

S3_MODULE = 'strategy3_vwap_bands'
S3_LABEL = 'S3_VWAP_Bands'
ATR_PERIOD = 14
ATR_TRAIL_MULT = 1.5


def _normalize_side(side):
    s = str(side).lower()
    if s in ('long', 'buy'):
        return 'long'
    return 'short'


def _is_s3_strategy(name):
    if not name:
        return False
    return name in (S3_MODULE, S3_LABEL)


def _fetch_atr(public_exchange, symbol, period=ATR_PERIOD):
    raw = public_exchange.fetch_ohlcv(symbol, '5m', limit=period + 5)
    df = pd.DataFrame(raw, columns=['ts', 'open', 'high', 'low', 'close', 'volume'])
    atr = ta.atr(df['high'], df['low'], df['close'], length=period)
    return float(atr.iloc[-1])


def _current_stop_loss(pos):
    sl = pos.get('stopLossPrice')
    if sl is not None:
        return float(sl)
    info = pos.get('info', {})
    if info.get('stopLoss'):
        return float(info['stopLoss'])
    return None


def _update_stop_loss(exchange, symbol, new_sl):
    market = exchange.market(symbol)
    req = {
        'category': 'linear',
        'symbol': market['id'],
        'positionIdx': 0,
        'tpslMode': 'Full',
        'slOrderType': 'Market',
        'stopLoss': exchange.price_to_precision(symbol, new_sl),
        'slTriggerBy': 'LastPrice',  # demo mark price spikes erratically
    }
    # Guard: don't try to set SL if position size is zero
    try:
        pos_info = exchange.fetch_positions([symbol])
        contracts = float(pos_info[0].get('contracts', 0) or 0) if pos_info else 0
        if contracts <= 0:
            logger.debug(f"Skipping SL update for {symbol} — no open position")
            return
    except Exception as e:
        # If fetch fails, still try to set SL but we'll catch error below
        pass

    exchange.privatePostV5PositionTradingStop(req)


def update_s3_trailing_stops(exchange, public_exchange, open_positions, conn=None):
    """
    Trail stop-loss on open S3 positions using 5m ATR.
    open_positions: list of ccxt position dicts with non-zero contracts.
    conn: optional sqlite connection to confirm strategy from signals table.
    """
    if not open_positions:
        return

    s3_symbols = set()
    if conn is not None:
        c = conn.cursor()
        c.execute(
            """SELECT symbol FROM signals
               WHERE status='open' AND strategy IN (?, ?)""",
            (S3_MODULE, S3_LABEL),
        )
        s3_symbols = {row[0] for row in c.fetchall()}

    for pos in open_positions:
        symbol = pos['symbol']
        if conn is not None:
            if symbol not in s3_symbols:
                continue
        else:
            meta = pos.get('info', {})
            if meta.get('strategy') and not _is_s3_strategy(meta.get('strategy')):
                continue

        contracts = float(pos.get('contracts', 0) or 0)
        if contracts <= 0:
            continue

        side = _normalize_side(pos.get('side'))
        mark = float(pos.get('markPrice') or pos.get('entryPrice') or 0)
        if mark <= 0:
            continue

        try:
            atr = _fetch_atr(public_exchange, symbol)
        except Exception as e:
            logger.warning(f"S3 trail ATR failed {symbol}: {e}")
            continue

        if side == 'long':
            new_sl = mark - ATR_TRAIL_MULT * atr
            current_sl = _current_stop_loss(pos)
            if current_sl is not None and new_sl <= current_sl:
                continue
        else:
            new_sl = mark + ATR_TRAIL_MULT * atr
            current_sl = _current_stop_loss(pos)
            if current_sl is not None and new_sl >= current_sl:
                continue

        try:
            _update_stop_loss(exchange, symbol, new_sl)
            logger.info(f"S3 trail SL {symbol} ({side}): {new_sl:.4f} (ATR {atr:.4f})")
        except Exception as e:
            err = str(e)
            if 'zero position' in err.lower() or '10001' in err:
                logger.debug(f"Skipping SL update — no position for {symbol}")
            else:
                logger.warning(f"S3 trail SL update failed {symbol}: {e}")


# ── M7: Trailing stops for non-S3 strategies ─────────────────────────────────
# Percentage-based trailing stop for S2 and S7. Trails the mark price by a
# configurable %; only moves the stop in the profitable direction (never
# widens the stop).
TRAIL_PCT = {
    'strategy2_breakout_retest': float(os.getenv('TRAIL_PCT_S2', '0.015')),   # 1.5%
    'strategy7_daily_momentum':  float(os.getenv('TRAIL_PCT_S7', '0.020')),   # 2.0%
}


def _trail_side(side):
    return 'long' if str(side).lower() in ('long', 'buy') else 'short'


def _pos_current_sl(pos):
    sl = pos.get('stopLossPrice')
    if sl is not None:
        return float(sl)
    info = pos.get('info', {})
    if info.get('stopLoss'):
        return float(info['stopLoss'])
    return None


def _pos_strategy(pos, conn, symbol):
    """Best-effort strategy lookup from signals table."""
    if conn is None:
        return None
    try:
        c = conn.cursor()
        c.execute(
            "SELECT strategy FROM signals WHERE symbol=? AND status='open' ORDER BY id DESC LIMIT 1",
            (symbol,),
        )
        row = c.fetchone()
        return row[0] if row else None
    except Exception:
        return None


def update_trailing_stops(exchange, public_exchange, open_positions, conn=None):
    """
    Trail stop-loss on open non-S3 positions using a fixed % below/above mark.

    Only moves the stop in the profitable direction (trailing up for longs,
    trailing down for shorts). Never widens the stop.
    """
    if not open_positions:
        return

    for pos in open_positions:
        symbol = pos['symbol']
        contracts = float(pos.get('contracts', 0) or 0)
        if contracts <= 0:
            continue

        strategy = _pos_strategy(pos, conn, symbol)
        if strategy not in TRAIL_PCT:
            continue

        side = _trail_side(pos.get('side'))
        mark = float(pos.get('markPrice') or pos.get('entryPrice') or 0)
        if mark <= 0:
            continue

        trail_pct = TRAIL_PCT[strategy]
        if side == 'long':
            new_sl = mark * (1 - trail_pct)
            current_sl = _pos_current_sl(pos)
            if current_sl is not None and new_sl <= current_sl:
                continue  # don't widen the stop
        else:
            new_sl = mark * (1 + trail_pct)
            current_sl = _pos_current_sl(pos)
            if current_sl is not None and new_sl >= current_sl:
                continue  # don't widen the stop

        try:
            market = exchange.market(symbol)
            req = {
                'category': 'linear',
                'symbol': market['id'],
                'positionIdx': 0,
                'tpslMode': 'Full',
                'slOrderType': 'Market',
                'stopLoss': exchange.price_to_precision(symbol, new_sl),
                'slTriggerBy': 'LastPrice',
            }
            # Guard: don't try to set SL if position size is zero
            try:
                pos_info = exchange.fetch_positions([symbol])
                contracts_check = float(pos_info[0].get('contracts', 0) or 0) if pos_info else 0
                if contracts_check <= 0:
                    logger.debug(f"Skipping trail SL update for {symbol} — no open position")
                    continue
            except Exception:
                pass

            exchange.privatePostV5PositionTradingStop(req)
            logger.info(
                f"Trail SL {symbol} ({strategy} {side}): {new_sl:.4f} "
                f"(trail {trail_pct*100:.1f}% of mark {mark:.4f})"
            )
        except Exception as e:
            err = str(e)
            if 'zero position' in err.lower() or '10001' in err:
                logger.debug(f"Skipping trail SL update — no position for {symbol}")
            else:
                logger.warning(f"Trail SL update failed {symbol}: {e}")
