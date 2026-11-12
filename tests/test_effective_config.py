"""Tests for Stage 2.1 — single-source-of-truth effective config."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from risk.risk_manager import (
    RiskManager,
    canonical_strategy_key,
    effective_strategy_risk,
    effective_notional_caps,
)
from utils.config_loader import reset as reset_config


def _fresh_rm():
    reset_config()
    return RiskManager(initial_balance=500.0)


class TestCanonicalKey:
    def test_label_form(self):
        assert canonical_strategy_key('S2_Breakout_Retest') == 'strategy2_breakout_retest'

    def test_module_form_passthrough(self):
        assert canonical_strategy_key('strategy7_daily_momentum') == 'strategy7_daily_momentum'

    def test_non_string(self):
        assert canonical_strategy_key(None) is None


class TestEffectiveValues:
    """2.1: the effective values for S2, S7."""

    def test_s2(self):
        rm = _fresh_rm()
        # micro_strategy_risk 1.5% beats base strategy_risk 0.8%
        assert rm.risk_pct_for_strategy('strategy2_breakout_retest') == 0.015
        assert rm.risk_pct_for_strategy('S2_Breakout_Retest') == 0.015
        assert rm.max_notional_pct_for_strategy('strategy2_breakout_retest') == 0.25
        assert rm.max_notional_pct_for_strategy('S2_Breakout_Retest') == 0.25

    def test_s7(self):
        rm = _fresh_rm()
        assert rm.risk_pct_for_strategy('strategy7_daily_momentum') == 0.008
        assert rm.risk_pct_for_strategy('S7_Daily_Momentum') == 0.008
        assert rm.max_notional_pct_for_strategy('strategy7_daily_momentum') == 0.12

    def test_unknown_strategy_gets_default(self):
        rm = _fresh_rm()
        assert rm.risk_pct_for_strategy('some_unknown_strategy') == 0.01
        assert rm.risk_pct_for_strategy(None) == 0.01

    def test_inactive_s3_dial_removed(self):
        # S3's 2.5% dial was marked inactive in config.yaml (2.1) — it now
        # falls back to the 1% default rather than a live-looking 2.5%.
        rm = _fresh_rm()
        assert rm.risk_pct_for_strategy('strategy3_vwap_bands') == 0.01
        assert rm.max_notional_pct_for_strategy('strategy3_vwap_bands') == 0.10

    def test_leverage_from_config(self):
        rm = _fresh_rm()
        assert rm.leverage == 10  # config.yaml execution.leverage

    def test_effective_config_covers_all_active(self):
        rm = _fresh_rm()
        eff = rm.effective_config()
        for key in ('strategy2_breakout_retest', 'strategy7_daily_momentum',
                    'default'):
            assert key in eff['strategy_risk']
        assert eff['leverage'] == 10
        assert eff['max_trade_risk_pct'] == 0.02


class TestEnvOverrides:
    def test_env_beats_phase_dict(self):
        # Env must win over trading_phase constants — regression guard for the
        # re-apply pass in effective_strategy_risk().
        os.environ['STRATEGY_RISK__STRATEGY2_BREAKOUT_RETEST'] = '0.02'
        try:
            reset_config()
            assert effective_strategy_risk()['strategy2_breakout_retest'] == 0.02
            rm = RiskManager(initial_balance=500.0)
            # hard cap still applies: min(0.02, 0.02) = 0.02
            assert rm.risk_pct_for_strategy('strategy2_breakout_retest') == 0.02
        finally:
            del os.environ['STRATEGY_RISK__STRATEGY2_BREAKOUT_RETEST']
            reset_config()

    def test_env_notional_cap(self):
        os.environ['NOTIONAL_CAP__DEFAULT'] = '0.20'
        try:
            reset_config()
            assert effective_notional_caps()['default'] == 0.20
        finally:
            del os.environ['NOTIONAL_CAP__DEFAULT']
            reset_config()


class TestConfigHash:
    def test_hash_is_stable_across_instances(self):
        from execution.db_manager import config_hash
        import execution.config as eng_config
        reset_config()
        h1 = config_hash(RiskManager(initial_balance=500.0), eng_config)
        reset_config()
        h2 = config_hash(RiskManager(initial_balance=500.0), eng_config)
        assert h1 == h2
        assert len(h1) == 16

    def test_hash_changes_with_dial(self):
        from execution.db_manager import config_hash
        import execution.config as eng_config
        reset_config()
        rm = RiskManager(initial_balance=500.0)
        h_before = config_hash(rm, eng_config)
        rm.strategy_risk['strategy2_breakout_retest'] = 0.03
        assert config_hash(rm, eng_config) != h_before

    def test_hash_works_without_risk_manager(self):
        from execution.db_manager import config_hash
        import execution.config as eng_config
        h = config_hash(None, eng_config)
        assert len(h) == 16

    def test_hash_fallback_for_plain_objects(self):
        # Objects without effective_config() (mocks) use the legacy path.
        from execution.db_manager import config_hash

        class Plain:
            risk_pct_for_strategy = staticmethod(lambda s: 0.01)

        assert len(config_hash(Plain(), None)) == 16
