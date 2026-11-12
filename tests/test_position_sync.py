"""Tests for execution.position_sync."""

import sys
import os
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from execution.position_sync import normalize_side, pnl_usdt


def test_normalize_side():
    assert normalize_side('long') == 'long'
    assert normalize_side('buy') == 'long'
    assert normalize_side('short') == 'short'
    assert normalize_side('sell') == 'short'
    assert normalize_side('LONG') == 'long'


def test_pnl_usdt_long():
    pnl = pnl_usdt('long', 100, 110, 1.0)
    assert pnl == 10.0


def test_pnl_usdt_short():
    pnl = pnl_usdt('short', 100, 90, 1.0)
    assert pnl == 10.0


def test_pnl_usdt_missing():
    assert pnl_usdt('long', 0, 110, 1.0) is None
    assert pnl_usdt('long', 100, 0, 1.0) is None
    assert pnl_usdt('long', 100, 110, 0) is None