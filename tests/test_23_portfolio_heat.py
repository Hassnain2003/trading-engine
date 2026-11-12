"""Tests for Stage 2.3 — portfolio heat uses stored planned_risk_usd."""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from risk.risk_manager import RiskManager


class TestStoredPlannedRiskHeat:
    """2.3: open positions contribute their stored planned_risk_usd (actual
    stop risk), falling back to nominal equity × dial for legacy/adopted rows."""

    def _rm(self, balance=500.0):
        rm = RiskManager(initial_balance=balance)
        rm.current_balance = balance
        return rm

    def test_uses_stored_planned_risk_usd(self):
        rm = self._rm()
        # Position with stored planned_risk_usd: $12 actual risk.
        rm.add_position('BTC/USDT:USDT', 'long', 82000, 81000, 84000, 0.12,
                        'o1', strategy='strategy2_breakout_retest',
                        planned_risk_usd=12.0)
        # At $500 equity with 6% cap, $12 = 2.4%. Adding a new S2 entry at
        # nominal 1.5% × $500 = $7.50 → total $19.50 = 3.9% → passes.
        ok, reason = rm.portfolio_heat_ok(
            'ETH/USDT:USDT', 'long', new_strategy='strategy2_breakout_retest',
            new_planned_risk_usd=7.50,
        )
        assert ok, reason
        assert 'portfolio heat' not in reason

    def test_blocks_on_stored_risk_over_cap(self):
        rm = self._rm()
        # Stored risk $28 = 5.6% at $500. New entry $7.50 → $35.50 = 7.1% > 6%.
        rm.add_position('BTC/USDT:USDT', 'long', 82000, 81440, 84000, 0.56,
                        'o1', strategy='strategy2_breakout_retest',
                        planned_risk_usd=28.0)
        ok, reason = rm.portfolio_heat_ok(
            'ETH/USDT:USDT', 'long', new_strategy='strategy2_breakout_retest',
            new_planned_risk_usd=7.50,
        )
        assert not ok
        assert 'portfolio heat' in reason

    def test_falls_back_to_nominal_when_stored_missing(self):
        rm = self._rm()
        # No planned_risk_usd → fall back to nominal dial (pre-2.3 behavior,
        # keeps legacy/adopted rows working).
        rm.add_position('BTC/USDT:USDT', 'long', 82000, 81000, 84000, 0.12,
                        'o1', strategy='strategy2_breakout_retest')
        ok, reason = rm.portfolio_heat_ok(
            'ETH/USDT:USDT', 'long', new_strategy='strategy2_breakout_retest',
            new_planned_risk_usd=7.50,
        )
        # Nominal: 1.5% × $500 = $7.50 × 2 positions = $15 = 3% → passes.
        assert ok, reason

    def test_zero_stored_risk_treated_as_missing(self):
        rm = self._rm()
        # planned_risk_usd=0 → fall back to nominal.
        rm.add_position('BTC/USDT:USDT', 'long', 82000, 81000, 84000, 0.12,
                        'o1', strategy='strategy2_breakout_retest',
                        planned_risk_usd=0)
        ok, reason = rm.portfolio_heat_ok(
            'ETH/USDT:USDT', 'long', new_strategy='strategy2_breakout_retest',
            new_planned_risk_usd=7.50,
        )
        assert ok, reason

    def test_new_entry_without_planned_risk_uses_nominal(self):
        rm = self._rm()
        rm.add_position('BTC/USDT:USDT', 'long', 82000, 81440, 84000, 0.56,
                        'o1', strategy='strategy2_breakout_retest',
                        planned_risk_usd=28.0)
        # Candidate without planned_risk_usd → nominal 1.5% × $500 = $7.50.
        # Stored $28 + $7.50 = $35.50 = 7.1% > 6% → blocked.
        ok, reason = rm.portfolio_heat_ok(
            'ETH/USDT:USDT', 'long', new_strategy='strategy2_breakout_retest',
        )
        assert not ok
        assert 'portfolio heat' in reason
