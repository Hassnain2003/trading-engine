"""Tests for the mainnet interlock (safe-by-default).

Mainnet startup must be refused unless BOTH ALLOW_MAINNET=true and
I_UNDERSTAND_THE_RISK=yes are set. Demo/sandbox is the default and needs
no flags.
"""
import importlib
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import execution.config as eng_config


def _reload_with(demo=None, allow=None, risk=None):
    for k, v in (('BYBIT_DEMO', demo), ('ALLOW_MAINNET', allow),
                 ('I_UNDERSTAND_THE_RISK', risk)):
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
    return importlib.reload(eng_config)


def test_demo_default_needs_no_flags():
    cfg = _reload_with(demo='true', allow=None, risk=None)
    refuse, reason = cfg.mainnet_consent_required()
    assert refuse is False
    assert reason == ''


def test_mainnet_without_consent_is_refused():
    cfg = _reload_with(demo='false', allow=None, risk=None)
    refuse, reason = cfg.mainnet_consent_required()
    assert refuse is True
    lower = reason.lower()
    assert 'mainnet' in lower
    assert 'allow_mainnet' in lower
    assert 'i_understand_the_risk' in lower


def test_mainnet_with_only_one_flag_is_still_refused():
    cfg = _reload_with(demo='false', allow='true', risk=None)
    assert cfg.mainnet_consent_required()[0] is True
    cfg = _reload_with(demo='false', allow=None, risk='yes')
    assert cfg.mainnet_consent_required()[0] is True


def test_mainnet_with_both_flags_is_allowed():
    cfg = _reload_with(demo='false', allow='true', risk='yes')
    refuse, reason = cfg.mainnet_consent_required()
    assert refuse is False
    assert reason == ''
