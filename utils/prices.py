"""Mainnet reference prices — testnet marks often diverge from real market."""


def mainnet_last(public_exchange, symbol):
    try:
        return float(public_exchange.fetch_ticker(symbol)['last'])
    except Exception:
        return None


def sanitized_exit_price(public_exchange, symbol, entry, raw_exit, max_deviation_pct=15.0):
    """
    Prefer mainnet last when testnet fill/mark is far from signal entry.
    Keeps demo P&L and gatekeeper stats honest.
    """
    if raw_exit is None:
        return mainnet_last(public_exchange, symbol)
    if not entry:
        return raw_exit

    if abs(raw_exit - entry) / entry * 100 <= max_deviation_pct:
        return raw_exit

    ref = mainnet_last(public_exchange, symbol)
    if ref is not None:
        return ref
    return raw_exit


def remap_stops_to_entry(signal_entry, pos_entry, side, signal_sl, signal_tp):
    """Preserve signal risk/reward ratios on the actual fill price (testnet-safe)."""
    if not all(v is not None for v in (signal_entry, pos_entry, signal_sl, signal_tp)):
        return signal_sl, signal_tp
    if signal_entry <= 0 or pos_entry <= 0:
        return signal_sl, signal_tp

    if side == 'long':
        sl_pct = (signal_entry - signal_sl) / signal_entry
        tp_pct = (signal_tp - signal_entry) / signal_entry
        return pos_entry * (1 - sl_pct), pos_entry * (1 + tp_pct)

    sl_pct = (signal_sl - signal_entry) / signal_entry
    tp_pct = (signal_entry - signal_tp) / signal_entry
    return pos_entry * (1 + sl_pct), pos_entry * (1 - tp_pct)
