"""Shared manual-close logic — used by scripts/close_all.py and the dashboard.

Closes positions with reduce-only market orders and writes a per-symbol
standdown flag so the agent stays flat on those coins until the next UTC-day
verdict (see the standdown check in execution/engine.py _try_entry).
"""

from datetime import datetime, timezone


def close_positions(demo, conn, coins=None):
    """coins: iterable of base symbols like {'BTC','ETH'} or None for all.
    Returns a list of result dicts."""
    results = []
    positions = [
        p for p in demo.fetch_positions()
        if float(p.get('contracts', 0) or 0) != 0
    ]
    if coins:
        coins = {c.upper() for c in coins}
        positions = [p for p in positions if p['symbol'].split('/')[0] in coins]

    today = datetime.now(timezone.utc).strftime('%Y-%m-%d')
    for p in positions:
        symbol = p['symbol']
        qty = abs(float(p['contracts']))
        side = str(p.get('side')).lower()
        upnl = float(p.get('unrealizedPnl') or 0)
        close_side = 'sell' if side == 'long' else 'buy'
        # Standdown is PER-STRATEGY: the strategy whose position was closed
        # can't re-enter this symbol until its signal actually changes (next
        # UTC day) — but other strategies stay free to act on genuinely new
        # setups on the same coin immediately.
        row = conn.execute(
            "SELECT strategy FROM signals WHERE symbol=? AND status='open' "
            "ORDER BY id DESC LIMIT 1", (symbol,),
        ).fetchone()
        strategy = row[0] if row else None
        key = f"standdown_{strategy}_{symbol}" if strategy else f"standdown_{symbol}"
        try:
            demo.create_order(symbol, 'market', close_side, qty, None, {'reduceOnly': True})
            conn.execute(
                "INSERT INTO kv_state (key, value) VALUES (?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, today),
            )
            results.append({'symbol': symbol, 'side': side, 'qty': qty,
                            'upnl': upnl, 'ok': True, 'error': None})
        except Exception as e:
            results.append({'symbol': symbol, 'side': side, 'qty': qty,
                            'upnl': upnl, 'ok': False, 'error': str(e)})
    conn.commit()
    return results
