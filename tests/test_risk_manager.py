"""Tests for risk.risk_manager."""
import os
import sys
from datetime import datetime, timezone, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from risk.risk_manager import RiskManager, MAX_TRADE_RISK_PCT, WEEKLY_KILL_SWITCH_PCT


def test_position_size_basic():
    rm = RiskManager(initial_balance=1000.0)
    # 1% risk = $10, stop distance $1000 -> raw_qty = 0.01.
    # But notional cap (10% of equity = $100) caps notional at $100 -> qty = 100/50000 = 0.002
    qty = rm.calculate_position_size(entry_price=50000, stop_loss=49000, side='long')
    assert qty == 0.002


def test_position_size_capped_by_notional():
    rm = RiskManager(initial_balance=1000.0)
    # Tight stop ($1): risk_per_unit = max(1, 250) = 250.
    # raw_qty = 10/250 = 0.04, notional = 0.04*50000 = $2000 > $100 cap.
    # Capped to 100/50000 = 0.002.
    qty = rm.calculate_position_size(entry_price=50000, stop_loss=49999, side='long')
    assert qty == 0.002


def test_position_size_zero_equity():
    rm = RiskManager(initial_balance=0.0)
    qty = rm.calculate_position_size(entry_price=50000, stop_loss=49000, side='long')
    assert qty == 0.0


def test_risk_pct_default():
    rm = RiskManager()
    assert rm.risk_pct_for_strategy('unknown_strategy') == 0.01


def test_risk_pct_strategy_specific():
    rm = RiskManager()
    # Micro phase overrides (from trading_phase.MICRO_STRATEGY_RISK_PCT)
    # take priority over base STRATEGY_RISK_PCT values.
    assert rm.risk_pct_for_strategy('strategy2_breakout_retest') == 0.015
    assert rm.risk_pct_for_strategy('strategy7_daily_momentum') == 0.008


def test_weekly_kill_switch_not_armed():
    rm = RiskManager(initial_balance=1000.0)
    now = datetime(2026, 1, 5, 12, 0, 0, tzinfo=timezone.utc)  # Monday
    killed = rm.check_weekly_kill_switch(equity=1000.0, now=now)
    assert killed is False
    assert rm.weekly_killed is False


def test_weekly_kill_switch_armed():
    rm = RiskManager(initial_balance=1000.0)
    # Set a high watermark, then drop equity by 10% (> 7% threshold)
    rm.weekly_high_water_mark = 1000.0
    rm.weekly_high_water_ts = datetime(2026, 1, 5, 12, 0, 0, tzinfo=timezone.utc)
    now = datetime(2026, 1, 5, 13, 0, 0, tzinfo=timezone.utc)
    killed = rm.check_weekly_kill_switch(equity=900.0, now=now)  # 10% DD
    assert killed is True
    assert rm.weekly_killed is True


def test_weekly_kill_switch_blocks_entry():
    rm = RiskManager(initial_balance=1000.0)
    rm.weekly_killed = True
    assert rm.can_open_trade() is False


def test_can_open_trade_no_equity():
    rm = RiskManager(initial_balance=0.0)
    assert rm.can_open_trade() is False