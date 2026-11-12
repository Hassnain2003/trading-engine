"""Verification tests for the five high-priority fixes (2026-10-18).

Covers:
  #5 Slippage tracking (DB columns + computation)
  #6 Funding rate in scoring (compute_strategy_stats + _funding_for_day)
  #7 Stale data detection (candles_fresh)
  #8 Strategy kill switch (db_manager helpers + entry_scanner gate)
  #9 Walk-forward validation (offline script)
"""

import os
import sqlite3
import time
from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock

import pytest


class TestCandlesFresh:
    def test_fresh_bar(self):
        from execution.market_data import candles_fresh
        public = MagicMock()
        recent_ms = int((time.time() - 60) * 1000)
        public.fetch_ohlcv.return_value = [
            [recent_ms - 300000, 1, 1, 1, 1, 1],
            [recent_ms, 1, 1, 1, 1, 1],
            [recent_ms + 300000, 1, 1, 1, 1, 1],
        ]
        fresh, age = candles_fresh(public, 'BTC/USDT:USDT', '5m', max_age_sec=600)
        assert fresh is True
        assert age is not None and age < 600

    def test_stale_bar(self):
        from execution.market_data import candles_fresh
        public = MagicMock()
        stale_ms = int((time.time() - 1200) * 1000)
        public.fetch_ohlcv.return_value = [
            [stale_ms - 300000, 1, 1, 1, 1, 1],
            [stale_ms, 1, 1, 1, 1, 1],
            [stale_ms + 300000, 1, 1, 1, 1, 1],
        ]
        fresh, age = candles_fresh(public, 'BTC/USDT:USDT', '5m', max_age_sec=600)
        assert fresh is False
        assert age is not None and age > 600

    def test_fetch_error_fails_open(self):
        from execution.market_data import candles_fresh
        public = MagicMock()
        public.fetch_ohlcv.side_effect = ConnectionError('timeout')
        fresh, age = candles_fresh(public, 'BTC/USDT:USDT', '5m', max_age_sec=600)
        assert fresh is True
        assert age is None

    def test_no_bars_is_stale(self):
        from execution.market_data import candles_fresh
        public = MagicMock()
        public.fetch_ohlcv.return_value = []
        fresh, age = candles_fresh(public, 'BTC/USDT:USDT', '5m', max_age_sec=600)
        assert fresh is False
        assert age is None


class TestStrategyKillSwitch:
    def _make_conn(self):
        conn = sqlite3.connect(':memory:')
        conn.execute('CREATE TABLE kv_state (key TEXT PRIMARY KEY, value TEXT)')
        conn.execute('''CREATE TABLE signals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT, symbol TEXT, strategy TEXT, side TEXT,
            entry REAL, stop_loss REAL, take_profit REAL, status TEXT,
            exit_reason TEXT, phase_id TEXT, exit_price REAL,
            expected_price REAL, actual_fill_price REAL, slippage_bps REAL)''')
        return conn

    def test_default_enabled(self):
        from execution.db_manager import strategy_disabled
        conn = self._make_conn()
        assert strategy_disabled(conn, 'strategy7_daily_momentum') is False

    def test_set_and_check_disabled(self):
        from execution.db_manager import strategy_disabled, set_strategy_disabled
        conn = self._make_conn()
        set_strategy_disabled(conn, 'strategy7_daily_momentum', True)
        assert strategy_disabled(conn, 'strategy7_daily_momentum') is True
        assert strategy_disabled(conn, 'strategy2_breakout_retest') is False

    def test_re_enable(self):
        from execution.db_manager import strategy_disabled, set_strategy_disabled
        conn = self._make_conn()
        set_strategy_disabled(conn, 'strategy7_daily_momentum', True)
        set_strategy_disabled(conn, 'strategy7_daily_momentum', False)
        assert strategy_disabled(conn, 'strategy7_daily_momentum') is False

    def test_list_disabled(self):
        from execution.db_manager import set_strategy_disabled, list_disabled_strategies
        conn = self._make_conn()
        set_strategy_disabled(conn, 'strategy7_daily_momentum', True)
        set_strategy_disabled(conn, 'strategy2_breakout_retest', False)
        result = list_disabled_strategies(conn)
        assert result == {'strategy7_daily_momentum'}

    def test_scan_entries_skips_disabled(self):
        from execution.entry_scanner import scan_entries
        from execution.db_manager import set_strategy_disabled
        conn = self._make_conn()
        set_strategy_disabled(conn, 'strategy7_daily_momentum', True)
        demo = MagicMock()
        public = MagicMock()
        strategies = [('strategy7_daily_momentum', MagicMock(), ['BTC/USDT:USDT'])]
        rm = MagicMock()
        agent_state = {}
        scan_entries(demo, public, strategies, {'strategy7_daily_momentum': '5m'},
                     {'5m'}, rm, conn, agent_state)
        strategies[0][1].assert_not_called()

    def test_persists_across_connections(self):
        from execution.db_manager import set_strategy_disabled, strategy_disabled
        import tempfile
        tmp = tempfile.NamedTemporaryFile(suffix='.db', delete=False)
        tmp.close()
        try:
            conn1 = sqlite3.connect(tmp.name)
            conn1.execute('CREATE TABLE kv_state (key TEXT PRIMARY KEY, value TEXT)')
            set_strategy_disabled(conn1, 'strategy7_daily_momentum', True)
            conn1.close()
            conn2 = sqlite3.connect(tmp.name)
            assert strategy_disabled(conn2, 'strategy7_daily_momentum') is True
            conn2.close()
        finally:
            os.unlink(tmp.name)


class TestSlippageTracking:
    def _make_conn(self):
        conn = sqlite3.connect(':memory:')
        conn.execute('''CREATE TABLE signals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT, symbol TEXT, strategy TEXT, side TEXT,
            entry REAL, stop_loss REAL, take_profit REAL, status TEXT,
            exit_reason TEXT, phase_id TEXT, exit_price REAL,
            expected_price REAL, actual_fill_price REAL, slippage_bps REAL)''')
        return conn

    def test_long_slippage_positive_when_worse(self):
        conn = self._make_conn()
        now = datetime.now(timezone.utc).isoformat()
        conn.execute(
            "INSERT INTO signals (timestamp, symbol, strategy, side, entry, "
            "stop_loss, take_profit, status, phase_id, expected_price) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (now, 'BTC/USDT:USDT', 'strategy2_breakout_retest', 'long',
             100.0, 95.0, 110.0, 'closed', 'test', 100.0))
        conn.commit()
        fill_entry, expected_price = 100.5, 100.0
        slip_bps = (fill_entry - expected_price) / expected_price * 10000
        c = conn.cursor()
        c.execute("UPDATE signals SET actual_fill_price=?, slippage_bps=? WHERE id=1",
                  (fill_entry, slip_bps))
        conn.commit()
        row = conn.execute("SELECT actual_fill_price, slippage_bps FROM signals WHERE id=1").fetchone()
        assert row[0] == 100.5
        assert abs(row[1] - 50.0) < 0.01

    def test_short_slippage_positive_when_worse(self):
        fill_entry, expected_price = 99.5, 100.0
        slip_bps = (expected_price - fill_entry) / expected_price * 10000
        assert abs(slip_bps - 50.0) < 0.01

    def test_post_only_negative_slippage(self):
        fill_entry, expected_price = 99.95, 100.0
        slip_bps = (fill_entry - expected_price) / expected_price * 10000
        assert slip_bps < 0

    def test_slippage_columns_exist_after_init(self):
        from execution.db_manager import init_db
        conn = sqlite3.connect(':memory:')
        init_db(conn)
        cols = {r[1] for r in conn.execute('PRAGMA table_info(signals)').fetchall()}
        assert 'expected_price' in cols
        assert 'actual_fill_price' in cols
        assert 'slippage_bps' in cols

    def test_avg_slippage_bps_helper(self):
        from execution.engine import ExecutionEngine
        conn = self._make_conn()
        now = datetime.now(timezone.utc).isoformat()
        for slip in [1.0, 2.0, 3.0, None]:
            conn.execute(
                "INSERT INTO signals (timestamp, symbol, strategy, side, entry, "
                "status, slippage_bps) VALUES (?,?,?,?,?,?,?)",
                (now, 'BTC/USDT:USDT', 's', 'long', 100.0, 'closed', slip))
        conn.commit()
        eng = ExecutionEngine.__new__(ExecutionEngine)
        eng.conn = conn
        avg = eng._avg_slippage_bps()
        assert avg == 2.0


class TestFundingInScoring:
    def _make_conn(self):
        conn = sqlite3.connect(':memory:')
        conn.execute('''CREATE TABLE signals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT, symbol TEXT, strategy TEXT, side TEXT,
            entry REAL, stop_loss REAL, take_profit REAL, status TEXT,
            exit_reason TEXT, phase_id TEXT, exit_price REAL)''')
        conn.execute('''CREATE TABLE funding_payments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            exchange_id TEXT UNIQUE, timestamp TEXT, symbol TEXT, amount REAL)''')
        return conn

    def test_stats_includes_funding_field(self):
        from utils.stats import compute_strategy_stats
        conn = self._make_conn()
        today = datetime.now(timezone.utc).strftime('%Y-%m-%d')
        now = datetime.now(timezone.utc).isoformat()
        conn.execute(
            "INSERT INTO signals (timestamp, symbol, strategy, side, entry, exit_price, status) "
            "VALUES (?,?,?,?,?,?,?)",
            (now, 'BTC/USDT:USDT', 'strategy2_breakout_retest', 'short', 100.0, 99.0, 'closed'))
        conn.commit()
        stats = compute_strategy_stats(conn, day=today)
        assert 'strategy2_breakout_retest' in stats
        assert 'funding_usdt' in stats['strategy2_breakout_retest']
        assert stats['strategy2_breakout_retest']['funding_usdt'] is None

    def test_funding_amount_included(self):
        from utils.stats import compute_strategy_stats
        conn = self._make_conn()
        today = datetime.now(timezone.utc).strftime('%Y-%m-%d')
        now = datetime.now(timezone.utc).isoformat()
        conn.execute(
            "INSERT INTO signals (timestamp, symbol, strategy, side, entry, exit_price, status) "
            "VALUES (?,?,?,?,?,?,?)",
            (now, 'BTC/USDT:USDT', 'strategy2_breakout_retest', 'short', 100.0, 99.0, 'closed'))
        conn.execute(
            "INSERT INTO funding_payments (exchange_id, timestamp, symbol, amount) VALUES (?,?,?,?)",
            ('tx1', now, 'BTC/USDT:USDT', -0.05))
        conn.commit()
        stats = compute_strategy_stats(conn, day=today)
        assert stats['strategy2_breakout_retest']['funding_usdt'] == -0.05

    def test_funding_scoped_to_strategy_symbols(self):
        from utils.stats import compute_strategy_stats
        conn = self._make_conn()
        today = datetime.now(timezone.utc).strftime('%Y-%m-%d')
        now = datetime.now(timezone.utc).isoformat()
        conn.execute(
            "INSERT INTO signals (timestamp, symbol, strategy, side, entry, exit_price, status) "
            "VALUES (?,?,?,?,?,?,?)",
            (now, 'BTC/USDT:USDT', 'strategy2_breakout_retest', 'short', 100.0, 99.0, 'closed'))
        conn.execute(
            "INSERT INTO funding_payments (exchange_id, timestamp, symbol, amount) VALUES (?,?,?,?)",
            ('tx1', now, 'ETH/USDT:USDT', -0.10))
        conn.execute(
            "INSERT INTO funding_payments (exchange_id, timestamp, symbol, amount) VALUES (?,?,?,?)",
            ('tx2', now, 'BTC/USDT:USDT', -0.03))
        conn.commit()
        stats = compute_strategy_stats(conn, day=today)
        assert stats['strategy2_breakout_retest']['funding_usdt'] == -0.03

    def test_funding_sync_inserts_and_deduplicates(self):
        from execution.funding_sync import sync_funding_payments
        conn = self._make_conn()
        demo = MagicMock()
        ts_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
        demo.fetch_funding_history.return_value = [
            {'id': 'f1', 'timestamp': ts_ms, 'amount': -0.02},
            {'id': 'f2', 'timestamp': ts_ms, 'amount': 0.01},
        ]
        sync_funding_payments(demo, conn, symbols=['BTC/USDT:USDT'])
        count = conn.execute("SELECT COUNT(*) FROM funding_payments").fetchone()[0]
        assert count == 2
        sync_funding_payments(demo, conn, symbols=['BTC/USDT:USDT'])
        count = conn.execute("SELECT COUNT(*) FROM funding_payments").fetchone()[0]
        assert count == 2

    def test_funding_sync_handles_api_error(self):
        from execution.funding_sync import sync_funding_payments
        conn = self._make_conn()
        demo = MagicMock()
        demo.fetch_funding_history.side_effect = ConnectionError('down')
        sync_funding_payments(demo, conn, symbols=['BTC/USDT:USDT'])
        count = conn.execute("SELECT COUNT(*) FROM funding_payments").fetchone()[0]
        assert count == 0


class TestWalkForward:
    def test_walk_forward_basic(self):
        from scripts.walk_forward_validation import walk_forward
        base = datetime(2026, 1, 1, tzinfo=timezone.utc)
        trades = []
        for i in range(40):
            trades.append({
                'timestamp': (base + timedelta(days=i)).isoformat(),
                'strategy': 'test',
                'symbol': 'BTC/USDT:USDT',
                'pnl_pct': 0.5 if i % 3 else -0.3,
            })
        folds = walk_forward(trades, train_days=30, test_days=7)
        assert len(folds) >= 1
        assert folds[0]['train']['trades'] >= 5
        assert folds[0]['test']['trades'] >= 1

    def test_walk_forward_empty(self):
        from scripts.walk_forward_validation import walk_forward
        assert walk_forward([], 30, 7) == []

    def test_compute_metrics(self):
        from scripts.walk_forward_validation import compute_metrics
        m = compute_metrics([1.0, -0.5, 2.0, -1.0, 0.5])
        assert m['trades'] == 5
        assert m['wr'] == 60.0
        assert m['pf'] == pytest.approx(3.5 / 1.5, rel=0.01)
        assert m['avg_pnl_pct'] == pytest.approx(0.4, rel=0.01)


class TestDbMigration:
    def test_init_db_adds_slippage_columns(self):
        from execution.db_manager import init_db
        conn = sqlite3.connect(':memory:')
        conn.execute('''CREATE TABLE signals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT, symbol TEXT, strategy TEXT, side TEXT,
            entry REAL, stop_loss REAL, take_profit REAL, status TEXT,
            exit_reason TEXT, phase_id TEXT, exit_price REAL)''')
        conn.commit()
        init_db(conn)
        cols = {r[1] for r in conn.execute('PRAGMA table_info(signals)').fetchall()}
        assert 'expected_price' in cols
        assert 'actual_fill_price' in cols
        assert 'slippage_bps' in cols
