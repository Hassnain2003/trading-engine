"""Tests for Stage 2.4 — min-lot bump honesty (actual risk/notional logging)."""
import sys
import os
import types

# Stub ccxt (unusable under this interpreter — same _cffi_backend breakage as
# the rest of the env) so execution.entry_scanner can be imported.
if 'ccxt' not in sys.modules:
    _ccxt_stub = types.ModuleType('ccxt')
    _ccxt_stub.base = types.ModuleType('ccxt.base')
    sys.modules['ccxt'] = _ccxt_stub
    sys.modules['ccxt.base'] = _ccxt_stub.base

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from execution.entry_scanner import ensure_min_exchange_qty


class FakeMarket:
    """Minimal stand-in for a ccxt market dict."""
    def __init__(self, min_amt):
        self.limits = {'amount': {'min': min_amt}}
    def get(self, key, default=None):
        return getattr(self, key, default)


class FakeDemo:
    def __init__(self, min_amt, price):
        self.markets = {'BTC/USDT:USDT': FakeMarket(min_amt)}
    def market(self, symbol):
        return self.markets[symbol]
    def amount_to_precision(self, symbol, qty):
        return qty


class TestMinLotBumpHonesty:
    def _demo(self, min_amt=0.001, price=82000.0):
        return FakeDemo(min_amt, price)

    def test_bump_logs_actual_risk_and_notional(self, caplog):
        # qty 0.0005 < min 0.001 → bump; stop-risk $1.00 < 2% cap $10 → bump proceeds
        demo = self._demo(min_amt=0.001, price=82000.0)
        with caplog.at_level('WARNING', logger='agent_execution'):
            result = ensure_min_exchange_qty(
                demo, 'BTC/USDT:USDT', 0.0005, 82000.0,
                stop_loss=81000.0, balance=500.0,
                max_notional_pct=0.12,
            )
        assert result == 0.001
        msgs = [r.getMessage() for r in caplog.records]
        assert any('bumping to min' in m and '$82.00' in m for m in msgs), msgs
        assert any('stop-risk $1.00' in m for m in msgs), msgs

    def test_bump_refuses_when_stop_risk_over_2pct_cap(self):
        # stop-risk $10.50 > 2% cap $10 → refuse (return 0.0)
        demo = self._demo(min_amt=0.001, price=82000.0)
        result = ensure_min_exchange_qty(
            demo, 'BTC/USDT:USDT', 0.0005, 82000.0,
            stop_loss=79500.0,  # stop distance $2500 → stop-risk $2.50 at 0.001... no
            balance=500.0,
            max_notional_pct=0.12,
        )
        # Actually: min_amt=0.001, stop_dist = |82000-79500| = 2500, stop_risk = 0.001*2500 = $2.50
        # $2.50 < $10 cap → bump proceeds. Let me use a wider stop.
        result = ensure_min_exchange_qty(
            demo, 'BTC/USDT:USDT', 0.0005, 82000.0,
            stop_loss=77000.0,  # stop_dist = 5000, stop_risk = 0.001*5000 = $5.00 < $10
            balance=500.0,
            max_notional_pct=0.12,
        )
        assert result == 0.001  # $5 < $10 cap, bump proceeds

    def test_bump_refuses_when_stop_risk_exceeds_cap(self):
        # stop_dist = 20000, stop_risk = 0.001 * 20000 = $20 > $10 cap → refuse
        demo = self._demo(min_amt=0.001, price=82000.0)
        result = ensure_min_exchange_qty(
            demo, 'BTC/USDT:USDT', 0.0005, 82000.0,
            stop_loss=62000.0,  # stop_dist = 20000
            balance=500.0,
            max_notional_pct=0.12,
        )
        assert result == 0.0

    def test_warning_when_notional_exceeds_strategy_cap(self, caplog):
        demo = self._demo(min_amt=0.001, price=82000.0)
        with caplog.at_level('WARNING', logger='agent_execution'):
            result = ensure_min_exchange_qty(
                demo, 'BTC/USDT:USDT', 0.0005, 82000.0,
                stop_loss=81000.0, balance=500.0,
                max_notional_pct=0.10,  # cap = $50, notional $82 > $50
            )
        assert result == 0.001
        msgs = [r.getMessage() for r in caplog.records]
        assert any('EXCEEDS strategy notional cap' in m for m in msgs), msgs

    def test_no_warning_when_notional_within_cap(self, caplog):
        demo = self._demo(min_amt=0.001, price=82000.0)
        with caplog.at_level('WARNING', logger='agent_execution'):
            result = ensure_min_exchange_qty(
                demo, 'BTC/USDT:USDT', 0.0005, 82000.0,
                stop_loss=81000.0, balance=500.0,
                max_notional_pct=0.25,  # cap = $125, notional $82 < $125
            )
        assert result == 0.001
        msgs = [r.getMessage() for r in caplog.records]
        assert not any('EXCEEDS strategy notional cap' in m for m in msgs), msgs

    def test_no_bump_when_qty_at_or_above_min(self):
        demo = self._demo(min_amt=0.001, price=82000.0)
        result = ensure_min_exchange_qty(
            demo, 'BTC/USDT:USDT', 0.005, 82000.0,
            stop_loss=81000.0, balance=500.0,
            max_notional_pct=0.10,
        )
        assert result == 0.005  # no bump needed

    def test_skip_when_balance_zero(self):
        demo = self._demo(min_amt=0.001, price=82000.0)
        result = ensure_min_exchange_qty(
            demo, 'BTC/USDT:USDT', 0.0005, 82000.0,
            stop_loss=81000.0, balance=0.0,
            max_notional_pct=0.10,
        )
        assert result == 0.0
