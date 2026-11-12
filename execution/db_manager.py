"""Database schema management, daily backup, and offsite push for the engine."""

import hashlib
import json
import os
import re
import sqlite3
import subprocess
from datetime import datetime, timezone

import requests


def init_db(conn):
    """Create all tables if they don't exist and ensure required columns."""
    # WAL mode: concurrent reads during writes (dashboard can read while
    # engine writes), survives crashes better than rollback journal.
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=5000")  # wait up to 5s on lock contention
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS signals (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        timestamp TEXT, symbol TEXT, strategy TEXT, side TEXT,
        entry REAL, stop_loss REAL, take_profit REAL, status TEXT,
        exit_reason TEXT, phase_id TEXT)''')
    c.execute('''CREATE TABLE IF NOT EXISTS orders (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        timestamp TEXT, symbol TEXT, order_id TEXT, type TEXT,
        price REAL, quantity REAL, status TEXT)''')
    c.execute('''CREATE TABLE IF NOT EXISTS equity_snapshots (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        timestamp TEXT, balance REAL, unrealized REAL, equity REAL)''')
    c.execute('''CREATE TABLE IF NOT EXISTS funding_payments (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        exchange_id TEXT UNIQUE, timestamp TEXT, symbol TEXT, amount REAL)''')
    c.execute('''CREATE TABLE IF NOT EXISTS kv_state (
        key TEXT PRIMARY KEY, value TEXT)''')
    c.execute('''CREATE TABLE IF NOT EXISTS ai_recommendations (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        timestamp TEXT, coin TEXT, status TEXT, reason TEXT)''')
    c.execute('''CREATE TABLE IF NOT EXISTS shadow_signals (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        timestamp TEXT, source TEXT, link TEXT UNIQUE, headline TEXT,
        coin TEXT, direction TEXT, severity INTEGER, reason TEXT,
        price_at_signal REAL, price_1h REAL, price_4h REAL, price_24h REAL)''')
    c.execute('''CREATE TABLE IF NOT EXISTS ab_news_signals (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        timestamp TEXT, model TEXT, link TEXT, headline TEXT,
        coin TEXT, direction TEXT, severity INTEGER, reason TEXT,
        price_at_signal REAL, price_1h REAL, price_4h REAL, price_24h REAL,
        UNIQUE(model, link, coin))''')
    c.execute('''CREATE TABLE IF NOT EXISTS harmonic_shadow_trades (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        timestamp TEXT, symbol TEXT, side TEXT, entry_price REAL,
        exit_price REAL, exit_time TEXT, status TEXT, exit_reason TEXT,
        season TEXT, season_day INTEGER, notional REAL, pnl_pct REAL, pnl_usdt REAL)''')
    c.execute('''CREATE TABLE IF NOT EXISTS exchange_fills (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        exec_id TEXT UNIQUE,
        order_id TEXT,
        symbol TEXT,
        side TEXT,
        qty REAL,
        price REAL,
        fee REAL,
        fee_currency TEXT,
        is_maker INTEGER,
        exec_time TEXT,
        closed_size REAL,
        raw_json TEXT
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS exchange_closed_pnl (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        order_id TEXT UNIQUE,
        symbol TEXT,
        side TEXT,
        qty REAL,
        avg_entry REAL,
        avg_exit REAL,
        closed_pnl REAL,
        open_fee REAL,
        close_fee REAL,
        closed_time TEXT,
        raw_json TEXT
    )''')
    _ensure_column(c, 'signals', 'exit_reason', 'TEXT')
    _ensure_column(c, 'signals', 'exit_price', 'REAL')
    _ensure_column(c, 'signals', 'phase_id', 'TEXT')
    _ensure_column(c, 'signals', 'expected_price', 'REAL')
    _ensure_column(c, 'signals', 'actual_fill_price', 'REAL')
    _ensure_column(c, 'signals', 'slippage_bps', 'REAL')
    # ── Stage 1.2: measurement-integrity columns (migration via ALTER TABLE) ──
    _ensure_column(c, 'signals', 'order_id', 'TEXT')
    _ensure_column(c, 'signals', 'entry_ts', 'TEXT')
    _ensure_column(c, 'signals', 'exit_ts', 'TEXT')
    _ensure_column(c, 'signals', 'entry_fill_price', 'REAL')
    _ensure_column(c, 'signals', 'entry_qty', 'REAL')
    _ensure_column(c, 'signals', 'entry_fee', 'REAL')
    _ensure_column(c, 'signals', 'exit_fill_price', 'REAL')
    _ensure_column(c, 'signals', 'exit_fee', 'REAL')
    _ensure_column(c, 'signals', 'funding_total', 'REAL')
    _ensure_column(c, 'signals', 'net_pnl_usd', 'REAL')
    _ensure_column(c, 'signals', 'planned_risk_usd', 'REAL')
    _ensure_column(c, 'signals', 'sl_attached_ok', 'INTEGER')
    _ensure_column(c, 'signals', 'size_multipliers_json', 'TEXT')
    _ensure_column(c, 'signals', 'git_hash', 'TEXT')
    _ensure_column(c, 'signals', 'config_hash', 'TEXT')
    conn.commit()


def _ensure_column(cursor, table, column, col_type):
    cursor.execute(f"PRAGMA table_info({table})")
    cols = {row[1] for row in cursor.fetchall()}
    if column not in cols:
        cursor.execute(f"ALTER TABLE {table} ADD COLUMN {column} {col_type}")


def verify_backup(path):
    """Run PRAGMA integrity_check on a backup file. Returns (ok, detail)."""
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        result = conn.execute("PRAGMA integrity_check").fetchone()
        conn.close()
        if result and result[0] == "ok":
            return True, "ok"
        return False, (result[0] if result else "empty result")
    except Exception as e:
        return False, str(e)


def backup_db_daily(conn, last_backup_date, logger=None):
    """Snapshot agent_trades.db once per UTC day into backups/ and push offsite.

    Returns the (possibly unchanged) last_backup_date.
    """
    today = datetime.now(timezone.utc).date()
    if last_backup_date == today:
        return last_backup_date

    backup_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'backups')
    os.makedirs(backup_dir, exist_ok=True)
    # Include phase ID in backup filename so phase journals are clearly separated.
    from trading_phase import PHASE_ID
    path = os.path.join(backup_dir, f"agent_trades_{PHASE_ID}_{today.isoformat()}.db")

    try:
        dest = sqlite3.connect(path)
        with dest:
            conn.backup(dest)
        dest.close()

        # Verify backup integrity before pushing offsite — a corrupt backup
        # is worse than no backup (gives false confidence at review time).
        ok, detail = verify_backup(path)
        if not ok:
            os.remove(path)  # discard the corrupt backup
            if logger:
                logger.warning(f"Backup integrity check FAILED ({detail}) — discarded {os.path.basename(path)}")
            return last_backup_date

        if logger:
            logger.info(f"DB backed up + verified → {os.path.basename(path)}")

        # Rotate: keep only the last 14 daily backups
        backups = sorted(f for f in os.listdir(backup_dir) if f.startswith('agent_trades_'))
        for old in backups[:-14]:
            os.remove(os.path.join(backup_dir, old))

        _push_backup_offsite(path, logger)
        return today
    except Exception as e:
        if logger:
            logger.warning(f"DB backup failed: {e}")
        return last_backup_date


def _push_backup_offsite(path, logger=None):
    """Push the daily backup through ntfy as an attachment (off-site vault)."""
    topic = os.getenv('NTFY_TOPIC', '').strip()
    if not topic:
        return
    try:
        size = os.path.getsize(path)
        if size > 1_800_000:  # stay under ntfy's free attachment limit
            if logger:
                logger.warning(f"DB backup too large for ntfy off-site ({size}b) — set up a VPS/rclone")
            return
        with open(path, 'rb') as f:
            resp = requests.put(
                f"https://ntfy.sh/{topic}",
                data=f,
                headers={
                    'Filename': os.path.basename(path),
                    'Title': 'Daily track-record backup',
                    'Priority': 'min',
                },
                timeout=30,
            )
        if resp.status_code == 200:
            if logger:
                logger.info("Off-site DB backup pushed via ntfy")
        else:
            if logger:
                logger.warning(f"Off-site backup HTTP {resp.status_code}")
    except Exception as e:
        if logger:
            logger.warning(f"Off-site backup failed: {e}")


# ── Strategy kill-switch ─────────────────────────────────────────────────────
# Stored in kv_state so it survives restarts. Key format: strategy_disabled:<mod_name>
# Value: '1' = disabled, '0' = enabled (absent = enabled by default).

def strategy_disabled(conn, mod_name):
    """True if the strategy has been disabled via the kill switch."""
    try:
        row = conn.execute(
            "SELECT value FROM kv_state WHERE key=?",
            (f'strategy_disabled:{mod_name}',),
        ).fetchone()
        return row is not None and row[0] in ('1', 'true', 'True')
    except Exception:
        return False


def set_strategy_disabled(conn, mod_name, disabled):
    """Persist a strategy kill-switch state to kv_state."""
    conn.execute(
        "INSERT OR REPLACE INTO kv_state (key, value) VALUES (?,?)",
        (f'strategy_disabled:{mod_name}', '1' if disabled else '0'),
    )
    conn.commit()


def list_disabled_strategies(conn):
    """Return the set of mod_names currently disabled via the kill switch."""
    try:
        rows = conn.execute(
            "SELECT key, value FROM kv_state WHERE key LIKE 'strategy_disabled:%'"
        ).fetchall()
        return {key.split(':', 1)[1] for key, val in rows if val in ('1', 'true', 'True')}
    except Exception:
        return set()


# ── Version / config hashing ──────────────────────────────────────────────────

def git_hash() -> str:
    """Current repo commit (short). 'unknown' when git is unavailable."""
    try:
        return subprocess.check_output(
            ['git', 'rev-parse', '--short', 'HEAD'],
            stderr=subprocess.DEVNULL,
            timeout=5,
        ).decode().strip()
    except Exception:
        return 'unknown'


def config_hash(risk_manager=None, eng_config=None) -> str:
    """Stable hash of the effective sizing/risk config so each signal can be
    tied back to the exact configuration that produced it.

    Primary source (2.1): RiskManager.effective_config() — the merged
    config.yaml + trading_phase + env view that actually governs sizing. JSON
    with sorted keys makes it deterministic across interpreter runs. The older
    per-attribute loop remains as fallback for objects without
    effective_config (tests/mocks) and for execution-engine constants.
    Reprs are normalized: callables reduce to their qualified name and
    memory addresses are stripped.
    """
    def _stable(val):
        if callable(val):
            return f"fn:{getattr(val, '__qualname__', getattr(val, '__name__', repr(val)))}"
        r = repr(val)
        r = re.sub(r'0x[0-9a-fA-F]+', '0xADDR', r)
        r = re.sub(r' at 0xADDR', '', r)
        return r

    h = hashlib.sha256()
    if risk_manager is not None:
        eff = getattr(risk_manager, 'effective_config', None)
        got = None
        if callable(eff):
            try:
                got = eff()
            except Exception:
                got = None
        if isinstance(got, dict):
            h.update(json.dumps(got, sort_keys=True).encode())
        else:
            for key in ('risk_pct_for_strategy', 'MAX_PORTFOLIO_RISK_PCT',
                        'MAX_CORRELATED_RISK_PCT', 'CORR_ENTRY_THRESHOLD',
                        'MAX_TRADE_RISK_PCT', 'DAILY_LOSS_LOCK_PCT',
                        'DAILY_FLATTEN_PCT', 'WEEKLY_KILL_SWITCH_PCT'):
                try:
                    val = getattr(risk_manager, key, None)
                except Exception:
                    val = None
                h.update(f'{key}={_stable(val)}|'.encode())
    if eng_config is not None:
        for key in ('DEFAULT_LEVERAGE', 'S3_LEVERAGE', 'PENDING_ORDER_TTL_SEC',
                    'STALE_BAR_MAX_AGE_SEC', 'MAX_POSITION_AGE_SEC',
                    'MAX_ENTRIES_PER_STRATEGY_PER_DAY', 'IOC_MAX_CROSS_PCT'):
            try:
                val = getattr(eng_config, key, None)
            except Exception:
                val = None
            h.update(f'{key}={_stable(val)}|'.encode())
    return h.hexdigest()[:16]


# ── Append-only exchange fill / closed-PnL ledger ─────────────────────────────

def _last_exec_time(conn) -> str:
    row = conn.execute("SELECT MAX(exec_time) FROM exchange_fills").fetchone()
    return row[0] if row and row[0] else None


def _last_closed_time(conn) -> str:
    row = conn.execute("SELECT MAX(closed_time) FROM exchange_closed_pnl").fetchone()
    return row[0] if row and row[0] else None


def sync_exchange_fills(demo, conn, symbols=None, logger=None):
    """Pull exchange execution fills since the last stored exec_time.

    Idempotent: exec_id is UNIQUE so re-syncs never duplicate rows. Never
    deletes. Returns the number of new fills inserted.
    """
    inserted = 0
    since = _last_exec_time(conn)
    since_ms = None
    if since:
        try:
            since_ms = int(datetime.fromisoformat(since).timestamp() * 1000)
        except Exception:
            since_ms = None

    targets = list(symbols or [])
    try:
        markets = demo.markets or demo.load_markets()
        targets = list(markets.keys())
    except Exception:
        pass

    before = conn.execute("SELECT COUNT(*) FROM exchange_fills").fetchone()[0]
    for symbol in targets:
        try:
            trades = demo.fetch_my_trades(symbol, since=since_ms, limit=1000)
        except Exception as e:
            if logger:
                logger.warning(f"fetch_my_trades failed for {symbol}: {e}")
            continue
        for t in trades or []:
            info = t.get('info') or {}
            exec_id = str(t.get('id') or info.get('execId') or '')
            if not exec_id:
                continue
            fee = t.get('fee') or {}
            fee_cost = fee.get('cost') if isinstance(fee, dict) else None
            fee_ccy = fee.get('currency') if isinstance(fee, dict) else None
            is_maker = t.get('takerOrMaker')
            is_maker_int = (
                1 if is_maker == 'maker' else (0 if is_maker == 'taker' else None)
            )
            try:
                conn.execute(
                    """INSERT OR IGNORE INTO exchange_fills
                       (exec_id, order_id, symbol, side, qty, price, fee,
                        fee_currency, is_maker, exec_time, closed_size, raw_json)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        exec_id,
                        str(t.get('order') or t.get('orderId') or ''),
                        symbol,
                        t.get('side'),
                        float(t.get('amount') or 0),
                        float(t.get('price') or 0),
                        float(fee_cost) if fee_cost is not None else None,
                        fee_ccy,
                        is_maker_int,
                        t.get('datetime'),
                        float(info.get('closedSize') or 0)
                        if info.get('closedSize') is not None
                        else None,
                        json.dumps(t, default=str),
                    ),
                )
            except Exception:
                pass
    conn.commit()
    after = conn.execute("SELECT COUNT(*) FROM exchange_fills").fetchone()[0]
    return max(0, after - before)


def sync_exchange_closed_pnl(demo, conn, logger=None):
    """Pull Bybit v5 closed-PnL records since the last stored closed_time.

    Idempotent: order_id is UNIQUE. Never deletes. Returns new rows inserted.
    """
    inserted = 0
    since = _last_closed_time(conn)
    since_ms = None
    if since:
        try:
            since_ms = int(datetime.fromisoformat(since).timestamp() * 1000)
        except Exception:
            since_ms = None

    try:
        resp = demo.private_get_v5_position_closed_pnl({
            'category': 'linear',
            'startTime': str(since_ms) if since_ms else '0',
            'limit': '100',
        })
    except Exception as e:
        if logger:
            logger.warning(f"position_closed_pnl sync failed: {e}")
        return 0

    result = resp.get('result') or {}
    rows = result.get('list') or []
    before = conn.execute("SELECT COUNT(*) FROM exchange_closed_pnl").fetchone()[0]
    for r in rows:
        try:
            conn.execute(
                """INSERT OR IGNORE INTO exchange_closed_pnl
                   (order_id, symbol, side, qty, avg_entry, avg_exit,
                    closed_pnl, open_fee, close_fee, closed_time, raw_json)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    r.get('orderId') or r.get('order_id') or '',
                    r.get('symbol'),
                    r.get('side'),
                    float(r.get('qty') or 0),
                    float(r.get('avgEntry') or r.get('entryPrice') or 0),
                    float(r.get('avgExit') or r.get('exitPrice') or 0),
                    float(r.get('closedPnl') or 0),
                    float(r.get('openFee') or 0),
                    float(r.get('closeFee') or 0),
                    r.get('closedTime') or r.get('updatedTime'),
                    json.dumps(r, default=str),
                ),
            )
        except Exception:
            pass
    conn.commit()
    after = conn.execute("SELECT COUNT(*) FROM exchange_closed_pnl").fetchone()[0]
    return max(0, after - before)