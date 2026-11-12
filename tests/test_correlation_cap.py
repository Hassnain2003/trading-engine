"""Tests for Stage 2.2 — correlation_cap_active lifecycle.

claude.scheduler imports claude.jobs → gemini_client → google.genai. The
dependency now installs and imports cleanly under this interpreter, so no
stub is needed (an earlier stub leaked into sys.modules and broke unrelated
tests in the same session).
"""
import os
import sys as _sys
from datetime import datetime, timezone

_sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from claude.scheduler import _on_new_utc_day, maybe_weekly_correlation
from risk.risk_manager import RiskManager


class _FakeMatrix:
    """Duck-typed stand-in for the pandas correlation matrix."""
    empty = False


def _state(rm, matrix=None):
    return {
        'risk_manager': rm,
        'correlation_matrix': matrix if matrix is not None else _FakeMatrix(),
        'last_correlation_week': None,
        '_scheduler_date': None,
        'paused': False,
        'conn': None,   # kv helpers no-op with conn=None
    }


def _sunday(hour=6):
    # 2026-10-25 is a Sunday
    return datetime(2026, 10, 25, hour, 0, 0, tzinfo=timezone.utc)


def test_high_correlation_sets_cap_and_limits_positions(monkeypatch):
    rm = RiskManager(initial_balance=500.0)
    state = _state(rm)
    monkeypatch.setattr('claude.scheduler.weekly_correlation_check',
                        lambda matrix: True)
    result = maybe_weekly_correlation(state, _sunday())
    assert result is True
    assert rm.correlation_cap_active is True
    assert rm._position_cap() == 2          # cap drops to 2 when active
    assert state['last_correlation_week'] == _sunday().strftime('%Y-W%W')


def test_low_correlation_clears_cap(monkeypatch):
    rm = RiskManager(initial_balance=500.0)
    rm.correlation_cap_active = True        # active from a previous verdict
    state = _state(rm)
    monkeypatch.setattr('claude.scheduler.weekly_correlation_check',
                        lambda matrix: False)
    result = maybe_weekly_correlation(state, _sunday())
    assert result is False
    assert rm.correlation_cap_active is False
    assert rm._position_cap() == rm.max_positions  # back to normal ceiling


def test_monday_rollover_clears_cap():
    rm = RiskManager(initial_balance=500.0)
    rm.correlation_cap_active = True
    state = _state(rm)
    state['_scheduler_date'] = datetime(2026, 10, 25, tzinfo=timezone.utc).date()
    monday = datetime(2026, 10, 26, 0, 0, 1, tzinfo=timezone.utc)
    assert monday.weekday() == 0
    _on_new_utc_day(state, monday)
    assert rm.correlation_cap_active is False
    assert state['last_correlation_week'] is None


def test_runs_once_per_week(monkeypatch):
    rm = RiskManager(initial_balance=500.0)
    state = _state(rm)
    calls = []
    monkeypatch.setattr('claude.scheduler.weekly_correlation_check',
                        lambda matrix: calls.append(1) or True)
    first = maybe_weekly_correlation(state, _sunday(6))
    second = maybe_weekly_correlation(state, _sunday(7))
    assert first is True
    assert second is None                   # already ran this week
    assert len(calls) == 1


def test_wrong_day_is_noop(monkeypatch):
    rm = RiskManager(initial_balance=500.0)
    state = _state(rm)
    monkeypatch.setattr('claude.scheduler.weekly_correlation_check',
                        lambda matrix: True)
    wednesday = datetime(2026, 10, 21, 12, 0, 0, tzinfo=timezone.utc)
    assert maybe_weekly_correlation(state, wednesday) is None
    assert rm.correlation_cap_active is False


def test_before_sunday_hour_is_noop(monkeypatch):
    rm = RiskManager(initial_balance=500.0)
    state = _state(rm)
    monkeypatch.setattr('claude.scheduler.weekly_correlation_check',
                        lambda matrix: True)
    assert maybe_weekly_correlation(state, _sunday(5)) is None
    assert rm.correlation_cap_active is False


def test_missing_matrix_marks_week_without_toggling(monkeypatch):
    rm = RiskManager(initial_balance=500.0)
    state = _state(rm, matrix=None)
    state['correlation_matrix'] = None
    result = maybe_weekly_correlation(state, _sunday())
    assert result is None                   # no verdict without a matrix
    assert rm.correlation_cap_active is False
    # week still marked so the job cannot re-fire every cycle
    assert state['last_correlation_week'] == _sunday().strftime('%Y-W%W')


def test_override_gate_stays_disabled():
    """2.2: check_correlation_override must NOT be enabled yet."""
    import pytest
    # entry_scanner imports ccxt transitively; skip when the env can't load it
    entry_scanner = pytest.importorskip('execution.entry_scanner',
                                        reason='ccxt unavailable in this env')
    assert entry_scanner.CHECK_CORRELATION_OVERRIDE is False
