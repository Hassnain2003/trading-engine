"""Tests for utils.config_loader."""
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from utils.config_loader import load, get, reset


def test_load_defaults():
    reset()
    cfg = load()
    assert cfg['risk']['daily_loss_lock_pct'] == 0.02
    assert cfg['risk']['weekly_kill_switch_pct'] == 0.07
    assert cfg['phase']['starting_equity'] == 0.0
    assert cfg['execution']['poll_interval_sec'] == 300
    assert cfg['gate']['profit_factor'] == 1.3


def test_get_convenience():
    reset()
    assert get('risk', 'daily_loss_lock_pct') == 0.02
    assert get('risk', 'nonexistent', 'DEFAULT') == 'DEFAULT'


def test_env_override():
    reset()
    os.environ['RISK__DAILY_LOSS_LOCK_PCT'] = '0.05'
    reset()
    assert get('risk', 'daily_loss_lock_pct') == 0.05
    del os.environ['RISK__DAILY_LOSS_LOCK_PCT']
    reset()
    assert get('risk', 'daily_loss_lock_pct') == 0.02


def test_bool_env_override():
    reset()
    os.environ['PHASE__FORCE_EQUITY'] = 'true'
    reset()
    assert get('phase', 'force_equity') is True
    del os.environ['PHASE__FORCE_EQUITY']
    reset()
    assert get('phase', 'force_equity') is False