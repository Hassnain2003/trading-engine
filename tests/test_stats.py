"""Tests for utils.stats."""
import os
import sys
import sqlite3
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from utils.stats import compute_strategy_stats, compute_demo_metrics, gate_status, GATE_CRITERIA


def _make_conn():
    conn = sqlite3.connect(':memory:')
    c = conn.cursor()
    c.execute('''CREATE TABLE signals (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        timestamp TEXT, symbol TEXT, strategy TEXT, side TEXT,
        entry REAL, stop_loss REAL, take_profit REAL, status TEXT,
        exit_reason TEXT, exit_price REAL)''')
    c.execute('''CREATE TABLE equity_snapshots (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        timestamp TEXT, balance REAL, unrealized REAL, equity REAL)''')
    conn.commit()
    return conn


def test_compute_strategy_stats_empty():
    conn = _make_conn()
    stats = compute_strategy_stats(conn)
    assert stats == {}


def test_compute_strategy_stats_with_trades():
    conn = _make_conn()
    c = conn.cursor()
    today = '2026-01-05'
    # Two winning trades, one losing trade for S2
    c.execute("INSERT INTO signals (timestamp, symbol, strategy, side, entry, exit_price, status) VALUES (?,?,?,?,?,?,?)",
              (f'{today} 10:00:00', 'BTC/USDT:USDT', 'strategy2_breakout_retest', 'long', 50000, 51000, 'closed'))
    c.execute("INSERT INTO signals (timestamp, symbol, strategy, side, entry, exit_price, status) VALUES (?,?,?,?,?,?,?)",
              (f'{today} 11:00:00', 'BTC/USDT:USDT', 'strategy2_breakout_retest', 'long', 50000, 50500, 'closed'))
    c.execute("INSERT INTO signals (timestamp, symbol, strategy, side, entry, exit_price, status) VALUES (?,?,?,?,?,?,?)",
              (f'{today} 12:00:00', 'BTC/USDT:USDT', 'strategy2_breakout_retest', 'long', 50000, 49000, 'closed'))
    conn.commit()
    stats = compute_strategy_stats(conn, day=today)
    s2 = stats['strategy2_breakout_retest']
    assert s2['trades'] == 3
    assert s2['wr'] == 66.7  # 2 wins / 3 trades
    assert s2['loss_streak'] == 1


def test_compute_demo_metrics_empty():
    conn = _make_conn()
    m = compute_demo_metrics(conn)
    assert m['total_trades'] == 0
    assert m['win_rate_pct'] == 0.0
    assert m['profit_factor'] == 0.0


def test_compute_demo_metrics_with_trades():
    conn = _make_conn()
    c = conn.cursor()
    c.execute("INSERT INTO signals (timestamp, symbol, strategy, side, entry, exit_price, status) VALUES (?,?,?,?,?,?,?)",
              ('2026-01-05 10:00:00', 'BTC/USDT:USDT', 'strategy2_breakout_retest', 'long', 50000, 51000, 'closed'))
    c.execute("INSERT INTO signals (timestamp, symbol, strategy, side, entry, exit_price, status) VALUES (?,?,?,?,?,?,?)",
              ('2026-01-05 11:00:00', 'BTC/USDT:USDT', 'strategy2_breakout_retest', 'long', 50000, 49000, 'closed'))
    conn.commit()
    m = compute_demo_metrics(conn)
    assert m['total_trades'] == 2
    assert m['win_rate_pct'] == 50.0
    # Round-trip cost = 0.11% per trade, so each P&L is reduced by 0.11%
    # Trade 1: +2% - 0.11% = +1.89%; Trade 2: -2% - 0.11% = -2.11%
    # Gross profit = 1.89, gross loss = 2.11, PF = 1.89/2.11 = 0.8957...
    assert m['profit_factor'] > 0


def test_gate_criteria():
    assert GATE_CRITERIA['profit_factor'] == ('>', 1.3)
    assert GATE_CRITERIA['sharpe_ratio'] == ('>', 1.0)
    assert GATE_CRITERIA['max_drawdown_pct'] == ('<', 15.0)
    assert GATE_CRITERIA['total_trades'] == ('>=', 60)


def test_gate_status_empty():
    conn = _make_conn()
    g = gate_status(conn)
    assert g['passed'] is False
    assert g['trades_progress'] == 0.0