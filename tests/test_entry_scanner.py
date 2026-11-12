"""Tests for execution.entry_scanner."""

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from execution.entry_scanner import symbol_busy


class FakeRiskManager:
    def __init__(self, positions=None):
        self.open_positions = positions or {}


class FakeCursor:
    def __init__(self, rows):
        self._rows = rows

    def execute(self, query, params):
        return self

    def fetchone(self):
        return self._rows[0] if self._rows else None


class FakeConn:
    def __init__(self, rows=None):
        self._rows = rows or []

    def cursor(self):
        return FakeCursor(self._rows)


def test_symbol_busy_position_exists():
    rm = FakeRiskManager({'BTC/USDT:USDT': {'side': 'long'}})
    conn = FakeConn([])
    assert symbol_busy(rm, conn, 'BTC/USDT:USDT') is True


def test_symbol_busy_signal_open():
    rm = FakeRiskManager({})
    conn = FakeConn([(1,)])
    assert symbol_busy(rm, conn, 'BTC/USDT:USDT') is True


def test_symbol_busy_free():
    rm = FakeRiskManager({})
    conn = FakeConn([])
    assert symbol_busy(rm, conn, 'ETH/USDT:USDT') is False