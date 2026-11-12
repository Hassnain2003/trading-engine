"""Tests for execution.order_manager."""

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from execution.order_manager import entry_limit_price


def test_entry_limit_price_long():
    price = entry_limit_price('long', 100000, 0.0002)
    assert price == 100000 * (1 - 0.0002)


def test_entry_limit_price_short():
    price = entry_limit_price('short', 100000, 0.0002)
    assert price == 100000 * (1 + 0.0002)