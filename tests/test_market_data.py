"""Tests for execution.market_data."""

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from execution.market_data import count_trades_today, ticker_price


def test_count_trades_today_empty():
    import sqlite3
    conn = sqlite3.connect(':memory:')
    c = conn.cursor()
    c.execute('''CREATE TABLE signals (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        timestamp TEXT, symbol TEXT, strategy TEXT, side TEXT,
        entry REAL, stop_loss REAL, take_profit REAL, status TEXT,
        exit_reason TEXT)''')
    conn.commit()
    assert count_trades_today(conn) == 0


def test_count_trades_today_with_trades():
    import sqlite3
    from datetime import datetime, timezone
    conn = sqlite3.connect(':memory:')
    c = conn.cursor()
    c.execute('''CREATE TABLE signals (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        timestamp TEXT, symbol TEXT, strategy TEXT, side TEXT,
        entry REAL, stop_loss REAL, take_profit REAL, status TEXT,
        exit_reason TEXT, exit_price REAL)''')
    today = datetime.now(timezone.utc).strftime('%Y-%m-%d')
    c.execute("INSERT INTO signals (timestamp, symbol, strategy, side, entry, exit_price, status) VALUES (?,?,?,?,?,?,?)",
              (f'{today} 10:00:00', 'BTC/USDT:USDT', 's2', 'long', 50000, 51000, 'closed'))
    c.execute("INSERT INTO signals (timestamp, symbol, strategy, side, entry, exit_price, status) VALUES (?,?,?,?,?,?,?)",
              (f'{today} 11:00:00', 'BTC/USDT:USDT', 's2', 'long', 50000, 49000, 'closed'))
    conn.commit()
    assert count_trades_today(conn) == 2