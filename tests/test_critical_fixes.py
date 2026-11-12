"""Verification tests for the four critical reliability fixes (2026-10-18)."""

import os
import sqlite3
import tempfile
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest


# ── Fix 4: ApiHealth circuit breaker ─────────────────────────────────────

class TestApiHealth:
    def test_initial_state_healthy(self):
        from execution.engine import ApiHealth
        h = ApiHealth()
        assert h.healthy
        assert h.status == 'ok'

    def test_trips_after_three_failures(self):
        from execution.engine import ApiHealth
        h = ApiHealth()
        assert h.record_failure('timeout 1') is None
        assert h.record_failure('timeout 2') is None
        assert h.record_failure('timeout 3') == 'tripped'
        assert not h.healthy

    def test_recovers_on_first_success(self):
        from execution.engine import ApiHealth
        h = ApiHealth()
        for i in range(3):
            h.record_failure(f'err {i}')
        assert not h.healthy
        assert h.record_success() == 'recovered'
        assert h.healthy

    def test_rate_limit_trips_immediately(self):
        from execution.engine import ApiHealth
        h = ApiHealth()
        assert h.record_failure('HTTP 429 too many requests') == 'tripped'
        assert not h.healthy

    def test_success_resets_counter(self):
        from execution.engine import ApiHealth
        h = ApiHealth()
        h.record_failure('e1')
        h.record_failure('e2')
        h.record_success()
        # Two more failures should NOT trip (counter was reset)
        assert h.record_failure('e3') is None
        assert h.record_failure('e4') is None
        assert h.record_failure('e5') == 'tripped'


# ── Fix 3: WAL mode + backup verification ────────────────────────────────

class TestWALAndBackup:
    def test_wal_mode_enabled(self):
        from execution.db_manager import init_db
        # WAL mode does not apply to :memory: databases — use a temp file.
        tmp = tempfile.NamedTemporaryFile(suffix='.db', delete=False)
        tmp.close()
        conn = sqlite3.connect(tmp.name)
        try:
            init_db(conn)
            mode = conn.execute('PRAGMA journal_mode').fetchone()[0]
            assert mode == 'wal'
            timeout = conn.execute('PRAGMA busy_timeout').fetchone()[0]
            assert timeout == 5000
        finally:
            conn.close()
            os.unlink(tmp.name)

    def test_verify_backup_valid(self):
        from execution.db_manager import verify_backup
        tmp = tempfile.NamedTemporaryFile(suffix='.db', delete=False)
        tmp.close()
        conn = sqlite3.connect(tmp.name)
        conn.execute('CREATE TABLE t(x)')
        conn.execute('INSERT INTO t VALUES (1)')
        conn.commit()
        conn.close()
        try:
            ok, detail = verify_backup(tmp.name)
            assert ok, detail
        finally:
            os.unlink(tmp.name)

    def test_verify_backup_corrupt(self):
        from execution.db_manager import verify_backup
        tmp = tempfile.NamedTemporaryFile(suffix='.db', delete=False)
        tmp.close()
        with open(tmp.name, 'wb') as f:
            f.write(b'not a sqlite database at all')
        try:
            ok, _ = verify_backup(tmp.name)
            assert not ok
        finally:
            os.unlink(tmp.name)



# ── Fix 2: portfolio_heat_ok ─────────────────────────────────────────────

class TestPortfolioHeat:
    def _rm(self, balance=500.0):
        from risk.risk_manager import RiskManager
        rm = RiskManager(initial_balance=balance)
        rm.current_balance = balance
        return rm

    def test_empty_book_passes(self):
        rm = self._rm()
        ok, reason = rm.portfolio_heat_ok('BTC/USDT:USDT', 'long',
                                          new_strategy='strategy2_breakout_retest')
        assert ok and reason == ''

    def test_no_equity_passes(self):
        rm = self._rm(balance=0.0)
        ok, reason = rm.portfolio_heat_ok('BTC/USDT:USDT', 'long')
        assert ok

    def test_portfolio_cap_blocks(self):
        rm = self._rm()
        # S2 dial = 1.5%; cap = 6%. 7 open + 1 new = 8*1.5% = 12% > 6%
        for i in range(7):
            rm.add_position(f'SYM{i}/USDT:USDT', 'long', 100, 95, 110, 1, f'o{i}',
                            strategy='strategy2_breakout_retest')
        ok, reason = rm.portfolio_heat_ok('NEW/USDT:USDT', 'long',
                                          new_strategy='strategy2_breakout_retest')
        assert not ok
        assert 'portfolio heat' in reason

    def test_portfolio_cap_passes_below_threshold(self):
        rm = self._rm()
        # S2 dial = 1.5% (MICRO_STRATEGY_RISK_PCT). 3 open + 1 new = 4*1.5% = 6%, not > 6%.
        for i in range(3):
            rm.add_position(f'SYM{i}/USDT:USDT', 'long', 100, 95, 110, 1, f'o{i}',
                            strategy='strategy2_breakout_retest')
        ok, reason = rm.portfolio_heat_ok('NEW/USDT:USDT', 'long',
                                          new_strategy='strategy2_breakout_retest')
        assert ok, reason

    def test_correlated_cluster_blocks(self):
        rm = self._rm()
        syms = [f'SYM{i}/USDT:USDT' for i in range(3)]
        for s in syms:
            rm.add_position(s, 'long', 100, 95, 110, 1, f'o_{s}',
                            strategy='strategy2_breakout_retest')
        matrix = {s: {'BTC/USDT:USDT': 0.9} for s in syms}
        matrix['BTC/USDT:USDT'] = {s: 0.9 for s in syms}
        # 3 * 1.5% + 1.5% = 6% > 3% correlated cap
        ok, reason = rm.portfolio_heat_ok('BTC/USDT:USDT', 'long',
                                          new_strategy='strategy2_breakout_retest',
                                          correlation_matrix=matrix)
        assert not ok
        assert 'correlated' in reason

    def test_uncorrelated_passes_cluster_check(self):
        rm = self._rm()
        syms = [f'SYM{i}/USDT:USDT' for i in range(3)]
        for s in syms:
            rm.add_position(s, 'long', 100, 95, 110, 1, f'o_{s}',
                            strategy='strategy2_breakout_retest')
        matrix = {s: {'BTC/USDT:USDT': 0.3} for s in syms}
        matrix['BTC/USDT:USDT'] = {s: 0.3 for s in syms}
        ok, reason = rm.portfolio_heat_ok('BTC/USDT:USDT', 'long',
                                          new_strategy='strategy2_breakout_retest',
                                          correlation_matrix=matrix)
        # Portfolio: 3*1.5 + 1.5 = 6%, not > 6% → passes; cluster corr < 0.7 → skipped
        assert ok, reason


# ── Fix 1: startup reconcile ─────────────────────────────────────────────

class TestStartupReconcile:
    def _make_conn(self):
        conn = sqlite3.connect(':memory:')
        # Mirrors the real signals schema (see execution/db_manager.py init_db),
        # including the Stage 1.2 measurement-integrity columns the engine
        # writes on adopt: entry_ts, entry_fill_price, entry_qty, sl_attached_ok.
        conn.execute('''CREATE TABLE signals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT, symbol TEXT, strategy TEXT, side TEXT,
            entry REAL, stop_loss REAL, take_profit REAL, status TEXT,
            exit_reason TEXT, phase_id TEXT, exit_price REAL,
            entry_ts TEXT, exit_ts TEXT, entry_fill_price REAL,
            entry_qty REAL, sl_attached_ok INTEGER, order_id TEXT,
            git_hash TEXT, config_hash TEXT)''')
        conn.execute('''CREATE TABLE orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT, symbol TEXT, order_id TEXT, type TEXT,
            price REAL, quantity REAL, status TEXT)''')
        conn.execute('CREATE TABLE kv_state (key TEXT PRIMARY KEY, value TEXT)')
        return conn

    def _make_engine(self, conn, positions):
        from execution.engine import ExecutionEngine
        eng = ExecutionEngine.__new__(ExecutionEngine)
        eng.demo = MagicMock()
        eng.demo.fetch_positions.return_value = positions
        eng.conn = conn
        eng.risk_manager = MagicMock()
        eng.agent_state = {}
        return eng

    def test_phantom_closed_orphan_adopted(self):
        conn = self._make_conn()
        now = datetime.now(timezone.utc).isoformat()
        conn.execute(
            "INSERT INTO signals (timestamp, symbol, strategy, side, entry, status, phase_id) "
            "VALUES (?,?,?,?,?,?,?)",
            (now, 'GONE/USDT:USDT', 'strategy2_breakout_retest', 'long', 100.0,
             'open', 'test_phase'))
        conn.commit()
        positions = [{'symbol': 'BTC/USDT:USDT', 'side': 'short', 'contracts': 0.001,
                      'entryPrice': 81036.4, 'stopLossPrice': None, 'takeProfitPrice': None}]
        eng = self._make_engine(conn, positions)

        with patch('execution.engine.send_alert') as mock_alert:
            result = eng._startup_reconcile()

        assert result is True
        assert eng.agent_state.get('startup_reconciled') is True

        row = conn.execute(
            "SELECT status, exit_reason FROM signals WHERE symbol='GONE/USDT:USDT'"
        ).fetchone()
        assert row == ('closed', 'engine_downtime')

        row = conn.execute(
            "SELECT status, strategy, side FROM signals WHERE symbol='BTC/USDT:USDT'"
        ).fetchone()
        assert row == ('open', 'adopted_orphan', 'short')
        eng.risk_manager.add_position.assert_called_once()
        assert mock_alert.called

    def test_in_sync_no_alert(self):
        conn = self._make_conn()
        positions = [{'symbol': 'BTC/USDT:USDT', 'side': 'short', 'contracts': 0.001,
                      'entryPrice': 81036.4, 'stopLossPrice': None, 'takeProfitPrice': None}]
        eng = self._make_engine(conn, positions)
        now = datetime.now(timezone.utc).isoformat()
        conn.execute(
            "INSERT INTO signals (timestamp, symbol, strategy, side, entry, status, phase_id) "
            "VALUES (?,?,?,?,?,?,?)",
            (now, 'BTC/USDT:USDT', 'strategy2_breakout_retest', 'short', 81036.4,
             'open', 'test_phase'))
        conn.commit()

        with patch('execution.engine.send_alert') as mock_alert:
            result = eng._startup_reconcile()

        assert result is True
        assert not mock_alert.called

    def test_exchange_unreachable_blocks(self):
        conn = self._make_conn()
        eng = self._make_engine(conn, [])
        eng.demo.fetch_positions.side_effect = ConnectionError('network down')

        with patch('execution.engine.send_alert') as mock_alert:
            result = eng._startup_reconcile()

        assert result is False
        assert not eng.agent_state.get('startup_reconciled')
        assert mock_alert.called
