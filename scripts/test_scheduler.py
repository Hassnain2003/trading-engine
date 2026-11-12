#!/usr/bin/env python3
"""Verify scheduler window logic and alert wiring (no network for scheduler tests)."""

import os
import sys
from datetime import datetime, timezone
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from claude.scheduler import _utc_at_or_after, run_scheduled_jobs
from risk.risk_manager import RiskManager


def test_utc_at_or_after():
    assert _utc_at_or_after(datetime(2026, 6, 20, 7, 44, tzinfo=timezone.utc), "07:45") is False
    assert _utc_at_or_after(datetime(2026, 6, 20, 7, 45, tzinfo=timezone.utc), "07:45") is True
    assert _utc_at_or_after(datetime(2026, 6, 20, 8, 0, tzinfo=timezone.utc), "07:45") is True
    assert _utc_at_or_after(datetime(2026, 6, 20, 20, 59, tzinfo=timezone.utc), "21:00") is False
    assert _utc_at_or_after(datetime(2026, 6, 20, 21, 30, tzinfo=timezone.utc), "21:00") is True
    print("PASS  _utc_at_or_after")


def test_pre_session_window():
    """After 07:45, pre-session should fire once even if poll missed exact minute."""
    state = {
        'pre_session_done_today': False,
        'post_session_done_today': True,
        'risk_manager': RiskManager(34000),
        'market_data': {'BTC/USDT:USDT': {'price': 1}},
        'strategy_stats': {},
        'trades_today': 0,
        'correlation_matrix': None,
        '_scheduler_date': datetime(2026, 6, 20, tzinfo=timezone.utc).date(),
    }
    now = datetime(2026, 6, 20, 7, 46, tzinfo=timezone.utc)
    with patch('claude.scheduler.datetime') as mock_dt:
        mock_dt.now.return_value = now
        mock_dt.side_effect = lambda *a, **k: datetime(*a, **k)
        with patch('claude.scheduler.pre_session_scan', return_value='{"BTC": {"status": "ACTIVE"}}'):
            with patch('claude.scheduler.parse_pre_session_status', return_value=(set(), set())):
                run_scheduled_jobs(state)
    assert state['pre_session_done_today'] is True
    print("PASS  pre-session fires at 07:46 (window, not exact match)")


def test_midnight_resume():
    state = {
        'pre_session_done_today': True,
        'post_session_done_today': True,
        'paused': True,
        'risk_manager': RiskManager(34000),
        'market_data': {},
        'strategy_stats': {},
        'trades_today': 0,
        '_scheduler_date': datetime(2026, 6, 19, tzinfo=timezone.utc).date(),
    }
    now = datetime(2026, 6, 20, 0, 5, tzinfo=timezone.utc)
    with patch('claude.scheduler.datetime') as mock_dt:
        mock_dt.now.return_value = now
        mock_dt.side_effect = lambda *a, **k: datetime(*a, **k)
        with patch('claude.scheduler.send_alert') as mock_alert:
            run_scheduled_jobs(state)
    assert state['paused'] is False
    assert state['pre_session_done_today'] is False
    mock_alert.assert_called_with("Trading resumed automatically at midnight.")
    print("PASS  midnight auto-resume + daily reset")


def test_weekly_sunday():
    state = {
        'pre_session_done_today': True,
        'post_session_done_today': True,
        'risk_manager': RiskManager(34000),
        'market_data': {},
        'strategy_stats': {},
        'trades_today': 0,
        'last_correlation_week': None,
        'correlation_matrix': None,
        '_scheduler_date': datetime(2026, 6, 21, tzinfo=timezone.utc).date(),  # Sunday
    }
    now = datetime(2026, 6, 21, 6, 30, tzinfo=timezone.utc)
    expected_week = now.strftime('%Y-W%W')
    with patch('claude.scheduler.datetime') as mock_dt:
        mock_dt.now.return_value = now
        mock_dt.side_effect = lambda *a, **k: datetime(*a, **k)
        run_scheduled_jobs(state)
    assert state['last_correlation_week'] == expected_week
    print("PASS  weekly correlation on Sunday >= 06:00")


def main():
    test_utc_at_or_after()
    test_pre_session_window()
    test_midnight_resume()
    test_weekly_sunday()
    print("\nAll scheduler tests passed.")


if __name__ == '__main__':
    main()
