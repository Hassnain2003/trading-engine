"""Safe-by-default: with default settings the engine places no orders.

ENABLE_TRADING defaults to off, so a fresh clone can never place an order
unintentionally. Entry scanning is gated on ENABLE_TRADING=true.
"""
import importlib
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from unittest.mock import MagicMock

import execution.config as eng_config
import execution.entry_scanner as escanner


def _reload_with(enable=None, demo='true'):
    for k, v in (('BYBIT_DEMO', demo), ('ENABLE_TRADING', enable)):
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
    importlib.reload(eng_config)
    importlib.reload(escanner)
    return escanner, eng_config


def test_enable_trading_defaults_off():
    _, cfg = _reload_with(enable=None)
    assert cfg.ENABLE_TRADING is False


def test_scan_entries_places_no_orders_when_trading_disabled():
    escanner, _ = _reload_with(enable=None)
    demo = MagicMock()
    public = MagicMock()
    strategies = [('strategy2_breakout_retest', MagicMock(), ['BTC/USDT:USDT'])]
    rm = MagicMock()
    agent_state = {}
    conn = MagicMock()
    escanner.scan_entries(
        demo, public, strategies,
        {'strategy2_breakout_retest': '5m'}, {'5m'},
        rm, conn, agent_state,
    )
    # Guard returns before the loop: no order method may be called.
    assert demo.place_limit_order.call_count == 0
    assert getattr(demo, 'place_s3_marketable_order', None) is None or \
        demo.place_s3_marketable_order.call_count == 0
    assert getattr(demo, 'set_leverage', None) is None or demo.set_leverage.call_count == 0


def test_scan_entries_proceeds_when_trading_enabled():
    escanner, _ = _reload_with(enable='true')
    assert escanner.ENABLE_TRADING is True
    # With trading enabled the guard does not short-circuit; we only assert the
    # module loaded with the flag on (full order flow needs a real venue).
    demo = MagicMock()
    public = MagicMock()
    strategies = [('strategy2_breakout_retest', MagicMock(), ['BTC/USDT:USDT'])]
    rm = MagicMock()
    rm.can_open_trade.return_value = False  # keep it from actually sizing
    agent_state = {}
    conn = MagicMock()
    escanner.scan_entries(
        demo, public, strategies,
        {'strategy2_breakout_retest': '5m'}, {'5m'},
        rm, conn, agent_state,
    )
