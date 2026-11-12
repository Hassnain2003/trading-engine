"""Execution engine — slimmed orchestrator.

Delegates to sub-modules:
  execution.config        — constants, STRATEGY_CONFIG, ACTIVE_STRATEGIES
  execution.db_manager    — schema, backup, offsite push
  execution.order_manager — order placement, SL/TP, pending reconcile, flatten
  execution.position_sync — live position reconciliation
  execution.entry_scanner — entry scan, _try_entry logic
  execution.market_data   — market data, correlation, ticker price
"""

import sys, os, time, sqlite3, logging, importlib, json, threading
from datetime import datetime, timezone, timedelta
from http.server import HTTPServer, BaseHTTPRequestHandler
from dotenv import load_dotenv
import ccxt

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from risk.risk_manager import RiskManager
from utils.manual_close import close_positions
from utils.config_loader import get as _cfg
from claude.notifier import send_alert, notify_trade_entry

from execution import config as eng_config
from execution.config import (
    STRATEGY_CONFIG, ACTIVE_STRATEGIES, STRATEGY_TIMEFRAME,
    DEMO_EXCLUDED_SYMBOLS, BAR_REFERENCE_SYMBOL, POLL_INTERVAL_SEC, HEALTH_PORT,
    STALE_BAR_MAX_AGE_SEC,
)
from execution.db_manager import (
    init_db, backup_db_daily, set_strategy_disabled, list_disabled_strategies,
    git_hash, config_hash,
)
from execution.market_data import (
    collect_market_data, refresh_correlation_matrix, count_trades_today, ticker_price,
    candles_fresh,
)
from execution.order_manager import (
    reconcile_pending_orders, flatten_for_daily_loss, alert_daily_lock_once,
    set_s3_leverage, set_default_leverage,
)
from execution.position_sync import (
    reconcile_live_positions, normalize_side, pnl_usdt, exit_price,
    filled_qty, close_and_alert, check_max_position_age,
)
from execution.entry_scanner import scan_entries, try_entry, ensure_min_exchange_qty, check_signal_reversal_exits
from execution.funding_sync import maybe_sync_funding

from trading_phase import (
    PHASE_ID, PHASE_LABEL, STARTING_EQUITY, FORCE_EQUITY,
    MIN_LOT_MAX_STOP_RISK_PCT, DB_FILENAME,
    phase_equity, set_wallet_baseline, get_wallet_baseline,
)

from claude.scheduler import run_scheduled_jobs
from claude.news_shadow import run_news_shadow
from execution.position_manager import update_s3_trailing_stops, update_trailing_stops
from execution.s7_manager import manage_s7_flips

load_dotenv()

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(message)s')
logger = logging.getLogger('agent_execution')

RESOLVER_REINIT_AFTER = 3
SELF_RESTART_AFTER = 6


class ApiHealth:
    """Exchange API circuit breaker.

    Tracks consecutive exchange-call failures. After FAIL_THRESHOLD, marks
    the API unhealthy (entries block) and sends one alert. Recovers
    automatically on the first successful call + sends a recovery alert.
    Rate-limit errors (429 / DDoS protection) also trip the breaker even
    if they come as singletons in rapid succession.
    """
    FAIL_THRESHOLD = 3

    def __init__(self):
        self.consecutive_failures = 0
        self.healthy = True
        self.last_error = None

    def record_success(self):
        self.consecutive_failures = 0
        if not self.healthy:
            self.healthy = True
            return 'recovered'   # caller sends recovery alert
        return None

    def record_failure(self, error):
        self.consecutive_failures += 1
        self.last_error = str(error)[:200]
        # Rate-limit hits trip immediately (they mean "back off NOW").
        is_rate_limit = any(tok in self.last_error.lower()
                            for tok in ('429', 'rate limit', 'too many', 'ddos'))
        if is_rate_limit:
            self.consecutive_failures = max(self.consecutive_failures, self.FAIL_THRESHOLD)
        if self.consecutive_failures >= self.FAIL_THRESHOLD and self.healthy:
            self.healthy = False
            return 'tripped'
        return None

    @property
    def status(self):
        return ('ok' if self.healthy
                else f'UNHEALTHY ({self.consecutive_failures} fails: {self.last_error})')


api_health = ApiHealth()   # module-level singleton shared across engine instance


class HealthHandler(BaseHTTPRequestHandler):
    """Minimal HTTP health endpoint: GET /health returns JSON status."""

    engine = None  # set by ExecutionEngine before starting the server

    def do_GET(self):
        if self.path == '/health':
            try:
                eq = HealthHandler.engine._current_equity()
                risk = HealthHandler.engine.risk_manager
                body = {
                    'status': 'ok',
                    'utc': datetime.now(timezone.utc).isoformat(),
                    'phase': PHASE_ID,
                    'equity': eq,
                    'daily_pnl_pct': risk.daily_pnl() if risk.equity_established else None,
                    'daily_loss_locked': risk.daily_loss_locked,
                    'daily_target_hit': risk.daily_target_hit,
                    'weekly_killed': risk.weekly_killed,
                    'open_positions': len(risk.open_positions),
                    'active_strategies': ACTIVE_STRATEGIES,
                    'api_health': api_health.status,
                    'startup_reconciled': bool(
                        HealthHandler.engine.agent_state.get('startup_reconciled')
                    ),
                    'stale_data': HealthHandler.engine.agent_state.get('_stale_data_alerted', False),
                    'disabled_strategies': sorted(list_disabled_strategies(HealthHandler.engine.conn)),
                    'avg_slippage_bps': HealthHandler.engine._avg_slippage_bps(),
                }
                payload = json.dumps(body).encode()
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
            except Exception as e:
                self.send_error(500, f'health check failed: {e}')
        elif self.path == '/':
            payload = b'Crypto Agent health endpoint. Use /health'
            self.send_response(200)
            self.send_header('Content-Type', 'text/plain')
            self.send_header('Content-Length', str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
        elif self.path.startswith('/kill_strategy/'):
            name = self.path.split('/kill_strategy/', 1)[1]
            set_strategy_disabled(self.server.engine.conn, name, True)
            body = json.dumps({'status': 'killed', 'strategy': name}).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path.startswith('/enable_strategy/'):
            name = self.path.split('/enable_strategy/', 1)[1]
            set_strategy_disabled(self.server.engine.conn, name, False)
            body = json.dumps({'status': 'enabled', 'strategy': name}).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_error(404)

    def log_message(self, format, *args):
        # Suppress default stderr logging for health checks
        pass


def _start_health_server(engine, port):
    """Run a background HTTP thread serving /health on the given port."""
    HealthHandler.engine = engine
    server = HTTPServer(('0.0.0.0', port), HealthHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True, name='health-server')
    thread.start()
    logger.info(f"Health endpoint listening on :{port}/health")


class ExecutionEngine:
    def __init__(self):
        # ── Mainnet interlock (safe-by-default) ──────────────────────────
        # Refuse to start on Bybit mainnet unless BOTH ALLOW_MAINNET=true and
        # I_UNDERSTAND_THE_RISK=yes are set, before touching the exchange.
        must_refuse, reason = eng_config.mainnet_consent_required()
        if must_refuse:
            msg = (
                "\n"
                "╔══════════════════════════════════════════════════════════════════╗\n"
                "║  MAINNET STARTUP REFUSED                                        ║\n"
                "╚══════════════════════════════════════════════════════════════════╝\n"
                + reason
                + "\n"
            )
            logger.error(reason)
            print(msg)
            raise SystemExit(2)

        self.public = ccxt.bybit(eng_config._BYBIT_COMMON)
        self.demo = ccxt.bybit({
            **eng_config._BYBIT_COMMON,
            'apiKey': os.getenv('BYBIT_API_KEY'),
            'secret': os.getenv('BYBIT_API_SECRET'),
            'options': {**eng_config._BYBIT_COMMON['options'], 'recvWindow': 20000},
        })
        self.demo.set_sandbox_mode(True)
        try:
            self.demo.load_time_difference()
        except Exception as e:
            logger.warning(f"Clock sync skipped: {e}")

        self.risk_manager = RiskManager()

        self.strategies = []
        for mod_name, coins in STRATEGY_CONFIG:
            if mod_name not in ACTIVE_STRATEGIES:
                continue
            try:
                mod = importlib.import_module(f'strategies.{mod_name}')
                func_names = [f'{mod_name.split("_", 1)[0]}_signal']
                func = next((getattr(mod, name, None) for name in func_names), None)
                if func is not None:
                    tradeable = [c for c in coins if c not in DEMO_EXCLUDED_SYMBOLS]
                    self.strategies.append((mod_name, func, tradeable))
                    logger.info(f"Loaded {mod_name} ({len(tradeable)} symbols)")
            except Exception as e:
                logger.error(f"Failed to load {mod_name}: {e}")

        self.conn = sqlite3.connect(DB_FILENAME)
        init_db(self.conn)
        self._configure_exchange()

        # 2.1: print the effective per-strategy config that actually governs
        # this run (config.yaml + trading_phase + env overrides), plus any
        # strategies paused via the kill switch, so a log line — not hope —
        # says what the engine will size with.
        logger.info(self.risk_manager.describe_effective_config())
        try:
            paused = sorted(list_disabled_strategies(self.conn))
            if paused:
                logger.info(f"Paused via strategy kill switch: {', '.join(paused)}")
        except Exception as e:
            logger.debug(f"Disabled-strategy list failed: {e}")

        self.agent_state = {
            'conn': self.conn,
            'pre_session_done_today': False,
            'post_session_done_today': False,
            'paused': False,
            'market_data': {},
            'strategy_stats': {},
            'trades_today': 0,
            'last_correlation_week': None,
            'correlation_matrix': None,
            'disabled_symbols': set(),
            'reduced_symbols': set(),
            'prev_live_symbols': set(),
            'live_snapshot': {},
            '_position_miss_counts': {},
            '_market_data_at': 0.0,
            '_correlation_at': 0.0,
            '_last_backup_date': None,
            '_funding_synced_at': 0.0,
        }
        self._last_bar_ts = {'5m': None, '15m': None}
        self._weekly_kill_alerted = False
        self._sync_pause_state()
        _start_health_server(self, HEALTH_PORT)

    # ── Setup ──────────────────────────────────────────────────────────────

    def _configure_exchange(self):
        symbols = set()
        for _, _, coins in self.strategies:
            symbols.update(coins)
        try:
            self.demo.load_markets()
        except Exception as e:
            logger.warning(f"load_markets skipped: {e}")

        for symbol in sorted(symbols):
            try:
                self.demo.set_leverage(eng_config.DEFAULT_LEVERAGE, symbol)
                logger.info(f"Leverage {eng_config.DEFAULT_LEVERAGE}x → {symbol}")
            except Exception as e:
                err = str(e)
                if '110043' in err or 'leverage not modified' in err.lower():
                    logger.info(f"Leverage already {eng_config.DEFAULT_LEVERAGE}x on {symbol}")
                else:
                    logger.warning(f"Leverage set failed for {symbol}: {e}")

        try:
            balance = self.demo.fetch_balance()
            usdt = balance.get('USDT', {}).get('total')
            if usdt is not None:
                usdt = float(usdt)
                self._ensure_wallet_baseline(usdt)
                eq = phase_equity(usdt)
                self.risk_manager.initial_balance = eq
                self.risk_manager.current_balance = eq
                self.risk_manager.session_start_equity = eq
                self._restore_session_start_equity()
                if FORCE_EQUITY and abs(usdt - STARTING_EQUITY) > 1:
                    logger.warning(
                        f"Wallet ${usdt:,.2f} mapped → phase equity ${eq:.2f} "
                        f"(baseline ${get_wallet_baseline():,.2f}). "
                        f"Reduce demo funds to ${STARTING_EQUITY:.0f} in Bybit UI, "
                        f"then set FORCE_EQUITY=0."
                    )
                logger.info(f"Account balance synced: wallet ${usdt:,.2f} → equity ${eq:,.2f} USDT")
                logger.info(
                    f"Phase {PHASE_ID} ({PHASE_LABEL}) — journal start ${STARTING_EQUITY:.2f}; "
                    f"daily target ~{self.risk_manager.daily_profit_target_pct*100:.0f}% "
                    f"(${self.risk_manager.daily_target_usdt():.2f})"
                )
        except Exception as e:
            logger.warning(f"Balance sync on init failed: {e}")

        if not self.risk_manager.equity_established:
            self._restore_session_start_equity()
        if not self.risk_manager.equity_established:
            logger.error(
                "Equity baseline NOT established (venue unreachable and no "
                "snapshot for today) — entries disabled and daily-loss breakers "
                "disarmed until a real balance read lands."
            )
            try:
                send_alert(
                    "⚠️ Agent started without a known equity — entries disabled "
                    "and daily-loss breakers disarmed until the venue responds."
                )
            except Exception:
                pass

    def _ensure_wallet_baseline(self, wallet_usdt):
        if not FORCE_EQUITY:
            return
        if get_wallet_baseline() is not None:
            return
        row = self.conn.execute(
            "SELECT value FROM kv_state WHERE key='wallet_baseline'"
        ).fetchone()
        if row and row[0]:
            set_wallet_baseline(float(row[0]))
            return
        set_wallet_baseline(wallet_usdt)
        self.conn.execute(
            "INSERT OR REPLACE INTO kv_state (key, value) VALUES (?, ?)",
            ("wallet_baseline", str(float(wallet_usdt))),
        )
        self.conn.commit()
        logger.info(f"Phase wallet baseline locked at ${float(wallet_usdt):,.2f}")

    def _restore_session_start_equity(self):
        today = datetime.now(timezone.utc).strftime('%Y-%m-%d')
        try:
            row = self.conn.execute(
                "SELECT equity FROM equity_snapshots WHERE timestamp LIKE ? ORDER BY id LIMIT 1",
                (f"{today}%",),
            ).fetchone()
        except Exception:
            return
        if row and row[0]:
            start = float(row[0])
            self.risk_manager.session_start_equity = start
            if (self.risk_manager.current_balance or 0) <= 0:
                self.risk_manager.current_balance = start
            logger.info(f"Session start equity restored from snapshot ${start:.2f}")

    def _sync_pause_state(self):
        try:
            row = self.conn.execute(
                "SELECT value FROM kv_state WHERE key='engine_paused'"
            ).fetchone()
        except Exception as e:
            logger.warning(f"Pause-state sync failed: {e}")
            return
        paused = str(row[0]).strip().lower() in ('1', 'true', 'yes', 'on') if row else False
        if paused != bool(self.agent_state.get('paused')):
            self.agent_state['paused'] = paused
            logger.warning(
                "Trading paused from dashboard control"
                if paused else
                "Trading resumed from dashboard control"
            )

    def _current_equity(self):
        """Return the most recent equity snapshot value, or None."""
        try:
            row = self.conn.execute(
                "SELECT equity FROM equity_snapshots ORDER BY id DESC LIMIT 1"
            ).fetchone()
            return float(row[0]) if row else None
        except Exception:
            return None

    # ── Maintenance cycle ──────────────────────────────────────────────────

    def _api_probe(self):
        """One cheap public-ticker call to drive the exchange API circuit breaker."""
        try:
            self.public.fetch_ticker(BAR_REFERENCE_SYMBOL)
            outcome = api_health.record_success()
            if outcome == 'recovered':
                msg = f"✅ Exchange API recovered after {api_health.last_error or 'failures'} — entries unblocked."
                logger.info(msg)
                send_alert(msg)
        except Exception as e:
            outcome = api_health.record_failure(e)
            if outcome == 'tripped':
                msg = (
                    f"🔴 Exchange API circuit breaker TRIPPED after "
                    f"{api_health.consecutive_failures} failures — new entries BLOCKED. "
                    f"Last error: {str(e)[:150]}"
                )
                logger.error(msg)
                send_alert(msg)

    def _run_maintenance(self):
        try:
            self._run_maintenance_inner()
        except Exception as e:
            err = str(e)
            if 'zero position' in err.lower() or '10001' in err:
                logger.debug(f"Suppressed zero-position error: {e}")
            else:
                logger.warning(f"Maintenance error: {e}")

    def _run_maintenance_inner(self):
        # Retry startup reconcile until it passes — entries stay blocked
        # (startup_reconciled flag) until exchange and DB agree.
        if not self.agent_state.get('startup_reconciled'):
            self._startup_reconcile()

        # Exchange API health probe — cheap public ticker read. This is the
        # signal source for the circuit breaker: 3 consecutive failures (or
        # any rate-limit hit) block new entries until recovery.
        self._api_probe()

        self._sync_pause_state()
        self.agent_state['market_data'] = collect_market_data(
            self.public, self.strategies, self.agent_state
        )
        self.agent_state['risk_manager'] = self.risk_manager
        self.agent_state['strategy_stats'] = __import__('utils.stats', fromlist=['compute_strategy_stats']).compute_strategy_stats(self.conn)
        self.agent_state['trades_today'] = count_trades_today(self.conn)
        try:
            from utils.live_tracker import build_report as build_tracker_report
            self.agent_state['tracker_report'] = build_tracker_report(self.conn)
        except Exception as e:
            logger.debug(f"Tracker report failed: {e}")
        self.agent_state['correlation_matrix'] = refresh_correlation_matrix(
            self.public, self.strategies, self.agent_state
        )
        try:
            run_scheduled_jobs(self.agent_state)
        except Exception as e:
            logger.warning(f"Scheduled jobs failed: {e}")
        try:
            reconcile_pending_orders(
                self.demo, self.conn, self.risk_manager,
                notify_trade_entry=notify_trade_entry,
                add_position=self.risk_manager.add_position,
            )
            try:
                all_coins = {s for _, _, coins in self.strategies for s in coins}
                maybe_sync_funding(self.demo, self.conn, self.agent_state, symbols=all_coins)
            except Exception as e:
                logger.warning(f"Funding sync skipped: {e}")
            try:
                from execution.db_manager import (
                    sync_exchange_fills, sync_exchange_closed_pnl,
                )
                _fills_n = sync_exchange_fills(
                    self.demo, self.conn, symbols=all_coins, logger=logger,
                )
                _pnl_n = sync_exchange_closed_pnl(
                    self.demo, self.conn, logger=logger,
                )
                if _fills_n or _pnl_n:
                    logger.info(
                        f"Exchange ledger sync: {_fills_n} fills, "
                        f"{_pnl_n} closed-PnL rows"
                    )
            except Exception as e:
                logger.warning(f"Exchange ledger sync failed: {e}")
            try:
                closed_n = check_max_position_age(
                    self.demo, self.conn, self.risk_manager, self.agent_state,
                    send_alert=send_alert,
                )
                if closed_n:
                    logger.info(f"Max-position-age closed {closed_n} position(s)")
            except Exception as e:
                logger.warning(f"Max position age check failed: {e}")
        except Exception as e:
            logger.warning(f"Pending reconcile failed: {e}")
        try:
            reconcile_live_positions(
                self.demo, self.public, self.conn, self.risk_manager, self.agent_state,
                send_alert=send_alert,
                check_weekly_kill_switch=self.risk_manager.check_weekly_kill_switch,
            )
            self._weekly_kill_alerted = getattr(self, '_weekly_kill_alerted', False)
        except Exception as e:
            logger.warning(f"Live position reconcile failed: {e}")
        try:
            positions = [
                p for p in self.demo.fetch_positions()
                if float(p.get('contracts', 0) or 0) != 0
            ]
            update_s3_trailing_stops(self.demo, self.public, positions, self.conn)
        except Exception as e:
            err = str(e)
            if 'zero position' not in err.lower() and '10001' not in err:
                logger.warning(f"S3 trailing stop update failed: {e}")
        try:
            update_trailing_stops(self.demo, self.public, positions, self.conn)
        except Exception as e:
            logger.warning(f"Trailing stop update failed: {e}")
        try:
            manage_s7_flips(self.demo, self.public, self.conn)
        except Exception as e:
            logger.warning(f"S7 flip manager failed: {e}")
        backup_db_daily(self.conn, self.agent_state.get('_last_backup_date'), logger=logger)
        self.agent_state['_last_backup_date'] = __import__('datetime', fromlist=['datetime']).datetime.now(timezone.utc).date()
        try:
            run_news_shadow(self.conn, self._ticker_price)
        except Exception as e:
            logger.warning(f"News shadow pipeline failed: {e}")

    def _ticker_price(self, symbol):
        return ticker_price(self.public, symbol)

    def _avg_slippage_bps(self, window_n=50):
        """Average slippage in bps across the most recent N filled signals."""
        try:
            row = self.conn.execute(
                "SELECT AVG(slippage_bps) FROM (" 
                "SELECT slippage_bps FROM signals WHERE slippage_bps IS NOT NULL " 
                "ORDER BY id DESC LIMIT ?)",
                (window_n,),
            ).fetchone()
            return round(row[0], 2) if row and row[0] is not None else None
        except Exception:
            return None

    def _write_health(self, status: str = 'ok'):
        try:
            with open('agent.health', 'w') as f:
                f.write(f"{datetime.now(timezone.utc).isoformat()}|{status}\n")
        except Exception:
            pass

    # ── Main loop ──────────────────────────────────────────────────────────

    @staticmethod
    def _reinit_resolver():
        try:
            import ctypes
            libc = ctypes.CDLL("libc.so.6")
            rc = getattr(libc, "__res_init")()
            logger.warning(f"Resolver re-init attempted (res_init rc={rc})")
        except Exception as e:
            logger.warning(f"Resolver re-init failed: {e}")

    # ── Startup reconciliation guard ──────────────────────────────────────────

    def _startup_reconcile(self):
        """Cross-check exchange positions vs DB open signals before entering the main loop.

        Orphan  = position on exchange with no open/pending signal in DB
                  (crash mid-entry, manual trade, previous-phase carryover).
                  → adopted into DB so SL/TP management and P&L tracking work.
        Phantom = open/pending signal in DB with no exchange position
                  (closed while engine was down, manual close elsewhere).
                  → marked closed with exit_reason='engine_downtime'.

        Returns True if reconciliation completed (safe to trade),
        False if exchange is unreachable (entries stay blocked by caller).
        """
        from execution.position_sync import normalize_side

        # 1) Fetch exchange positions
        try:
            positions = [
                p for p in self.demo.fetch_positions()
                if float(p.get('contracts', 0) or 0) != 0
            ]
        except Exception as e:
            logger.error(f"Startup reconcile: cannot reach exchange — {e}")
            send_alert(
                f"🔴 Startup reconcile FAILED — exchange unreachable: {str(e)[:150]}. "
                "New entries are BLOCKED until exchange responds."
            )
            return False

        exchange_syms = {p['symbol'] for p in positions}

        # 2) Fetch DB open/pending signals
        c = self.conn.cursor()
        c.execute(
            "SELECT id, symbol, side, entry, strategy FROM signals "
            "WHERE status IN ('pending','open')"
        )
        db_open = {row[1]: row for row in c.fetchall()}
        db_syms = set(db_open.keys())

        orphans = exchange_syms - db_syms     # on exchange, not in DB
        phantoms = db_syms - exchange_syms    # in DB, not on exchange

        # 3) Adopt orphans — insert as open signals so risk/sizing/close logic sees them
        adopted = []
        for p in positions:
            sym = p['symbol']
            if sym not in orphans:
                continue
            side = normalize_side(p.get('side'))
            entry = float(p.get('entryPrice') or p.get('average') or 0)
            qty = float(p.get('contracts', 0) or 0)
            now_iso = datetime.now(timezone.utc).isoformat()
            # SL/TP unknown for orphan — use exchange-native values if present.
            sl = float(p.get('stopLossPrice') or 0) or None
            tp = float(p.get('takeProfitPrice') or 0) or None
            sl_attached_ok = 1 if (sl and tp) else 0
            c.execute(
                '''INSERT INTO signals
                   (timestamp, symbol, strategy, side, entry, stop_loss, take_profit,
                    status, exit_reason, phase_id, entry_ts, entry_fill_price,
                    entry_qty, sl_attached_ok, git_hash, config_hash)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                (
                    now_iso, sym, 'adopted_orphan', side, entry, sl, tp,
                    'open', 'adopted_from_exchange', PHASE_ID,
                    now_iso, entry, qty, sl_attached_ok,
                    git_hash(), config_hash(self.risk_manager, eng_config),
                ),
            )
            sig_id = c.lastrowid
            # Record the adopted position as a filled order so the fill
            # ledger is complete for reconciliations.
            c.execute(
                '''INSERT INTO orders
                   (timestamp, symbol, order_id, type, price, quantity, status)
                   VALUES (?,?,?,?,?,?,?)''',
                (now_iso, sym, f'adopted_{sig_id}', 'adopted', entry, qty, 'filled'),
            )
            c.execute(
                "UPDATE signals SET order_id=? WHERE id=?",
                (f'adopted_{sig_id}', sig_id),
            )
            self.conn.commit()
            # Register with risk manager so sizing/caps account for it.
            try:
                _planned_risk_usd = qty * abs(entry - sl) if qty and entry and sl else None
            except (TypeError, ValueError):
                _planned_risk_usd = None
            self.risk_manager.add_position(
                sym, side, entry, sl, tp, qty, f'adopted_{sig_id}',
                strategy='adopted_orphan',
                planned_risk_usd=_planned_risk_usd,
            )
            adopted.append(f"{sym} {side} {qty} @ {entry:.4f}")
            logger.warning(f"Adopted orphan position {sym} ({side} {qty} @ {entry:.4f})")

        # 4) Close phantoms — position vanished while engine was down
        phantom_closed = []
        for sym, (sig_id, _, side, entry, strat) in db_open.items():
            if sym not in phantoms:
                continue
            c.execute(
                "UPDATE signals SET status='closed', exit_reason='engine_downtime' WHERE id=?",
                (sig_id,),
            )
            self.risk_manager.remove_position(sym)
            phantom_closed.append(f"{sym} ({strat})")
            logger.warning(
                f"Phantom signal {sig_id} {sym} — position not on exchange, "
                f"marked closed (engine_downtime)"
            )
        self.conn.commit()

        # 5) Alert if anything was reconciled
        if adopted or phantom_closed:
            parts = []
            if adopted:
                parts.append(f"Adopted {len(adopted)} orphan(s): {', '.join(adopted)}")
            if phantom_closed:
                parts.append(f"Closed {len(phantom_closed)} phantom(s): {', '.join(phantom_closed)}")
            msg = "🔧 Startup reconcile:\n" + "\n".join(parts)
            logger.warning(msg)
            send_alert(msg)
        else:
            logger.info("Startup reconcile: exchange and DB in sync — 0 orphans, 0 phantoms")

        self.agent_state['startup_reconciled'] = True
        return True

    def start(self):
        # ── Paper/demo banner (safe-by-default) ──────────────────────────
        venue = "BYBIT DEMO / SANDBOX" if eng_config.BYBIT_DEMO else "BYBIT MAINNET"
        trading = "ENABLED" if eng_config.ENABLE_TRADING else "DISABLED (set ENABLE_TRADING=true to place orders)"
        banner = (
            "\n"
            "╔══════════════════════════════════════════════════════════════════╗\n"
            f"║  PAPER / DEMO — {venue:<38} trading: {trading:<36}║\n"
            "║  Educational/engineering project — not financial advice.         ║\n"
            "╚══════════════════════════════════════════════════════════════════╝\n"
        )
        print(banner)
        logger.info(banner.replace('║', '').replace('╔', '').replace('╚', '').replace('╗', '').replace('╝', '').replace('═', '-'))
        logger.info(
            f"Agent started — bar-close entries, poll {POLL_INTERVAL_SEC}s, "
            f"leverage {eng_config.DEFAULT_LEVERAGE}x (S3 {eng_config.S3_LEVERAGE}x), "
            f"limit offset {eng_config.LIMIT_OFFSET_PCT:.4%}, pending TTL {eng_config.PENDING_ORDER_TTL_SEC}s"
        )
        # Startup reconciliation: exchange vs DB must agree before we trade.
        if not self._startup_reconcile():
            # Exchange unreachable — keep retrying reconcile inside maintenance
            # until it succeeds; entries stay blocked because we skip scans below.
            logger.error("Startup reconcile blocked — retrying every poll cycle until exchange responds")
        consecutive_errors = 0
        while True:
            try:
                self._run_maintenance()

                # Entries only run when startup reconcile has passed AND the
                # exchange API circuit breaker is closed (healthy).
                if (not self.agent_state.get('paused')
                        and self.agent_state.get('startup_reconciled')
                        and api_health.healthy):
                    # ── M8: Signal reversal exits (always active) ──
                    closed_n = check_signal_reversal_exits(
                        self.demo, self.public, self.strategies, self.risk_manager,
                        self.conn, self.agent_state, send_alert=send_alert,
                    )
                    if closed_n:
                        logger.info(f"Signal reversal closed {closed_n} position(s)")

                    from utils.bars import last_closed_bar_ts
                    bar_5m = last_closed_bar_ts(self.public, BAR_REFERENCE_SYMBOL, '5m')
                    bar_15m = last_closed_bar_ts(self.public, BAR_REFERENCE_SYMBOL, '15m')

                    # Stale data gate: skip entries when the reference bar is frozen
                    stale = False
                    if bar_5m:
                        bar_age = time.time() - bar_5m / 1000
                        stale = bar_age > STALE_BAR_MAX_AGE_SEC
                        if stale:
                            if not self.agent_state.get('_stale_data_alerted'):
                                msg = (f'⚠️ STALE DATA: {BAR_REFERENCE_SYMBOL} 5m candle is '
                                       f'{bar_age:.0f}s old (threshold {STALE_BAR_MAX_AGE_SEC}s). '
                                       'Entries paused until data recovers.')
                                logger.warning(msg)
                                send_alert(msg)
                                self.agent_state['_stale_data_alerted'] = True
                        elif self.agent_state.get('_stale_data_alerted'):
                            send_alert('✅ Market data fresh again — entries resumed.')
                            self.agent_state['_stale_data_alerted'] = False

                    if not stale:
                        if bar_5m and bar_5m != self._last_bar_ts['5m']:
                            self._last_bar_ts['5m'] = bar_5m
                            scan_entries(
                                self.demo, self.public, self.strategies, STRATEGY_TIMEFRAME, {'5m'},
                                self.risk_manager, self.conn, self.agent_state,
                                send_alert=send_alert,
                                notify_trade_entry=notify_trade_entry,
                                add_position=self.risk_manager.add_position,
                            )

                        if bar_15m and bar_15m != self._last_bar_ts['15m']:
                            self._last_bar_ts['15m'] = bar_15m
                            scan_entries(
                                self.demo, self.public, self.strategies, STRATEGY_TIMEFRAME, {'15m'},
                                self.risk_manager, self.conn, self.agent_state,
                                send_alert=send_alert,
                                notify_trade_entry=notify_trade_entry,
                                add_position=self.risk_manager.add_position,
                            )

                    # ── M1: Slippage alert ──
                # Alert once per alert window if average slippage exceeds threshold.
                SLIPPAGE_ALERT_BPS = 5.0
                SLIPPAGE_ALERT_WINDOW = 600  # seconds between slippage alerts
                now_ts = time.time()
                last_slip_alert = self.agent_state.get('_last_slippage_alert', 0)
                try:
                    avg_slip = self._avg_slippage_bps(window_n=50)
                    if avg_slip is not None and avg_slip > SLIPPAGE_ALERT_BPS:
                        if now_ts - last_slip_alert > SLIPPAGE_ALERT_WINDOW:
                            msg = (
                                f'⚠️ HIGH SLIPPAGE: avg {avg_slip:.1f} bps over last 50 fills '
                                f'(threshold {SLIPPAGE_ALERT_BPS:.0f} bps). '
                                f'Check limit offset / market depth.'
                            )
                            logger.warning(msg)
                            send_alert(msg)
                            self.agent_state['_last_slippage_alert'] = now_ts
                except Exception as e:
                    logger.debug(f'Slippage alert check skipped: {e}')

                consecutive_errors = 0
                self._write_health('ok')

            except Exception as e:
                consecutive_errors += 1
                self._write_health('error')
                logger.error(f"Main loop error ({consecutive_errors} consecutive): {e}", exc_info=True)
                if consecutive_errors == RESOLVER_REINIT_AFTER:
                    self._reinit_resolver()
                elif consecutive_errors >= SELF_RESTART_AFTER:
                    try:
                        send_alert(
                            f"⚠️ Agent self-restarting after {consecutive_errors} "
                            f"consecutive errors (last: {str(e)[:150]}). "
                            "Open positions keep their exchange-side SL/TP."
                        )
                    except Exception:
                        pass
                    logger.error("Exiting for a clean restart (run under deploy/run_agent_loop.sh or systemd)")
                    os._exit(42)

            time.sleep(POLL_INTERVAL_SEC)

    # ── Legacy / test entry point ──────────────────────────────────────────

    def run_cycle(self):
        """One maintenance tick + optional entry scan (legacy / tests)."""
        self._run_maintenance()
        if not self.agent_state.get('paused'):
            scan_entries(
                self.demo, self.public, self.strategies, STRATEGY_TIMEFRAME, {'5m', '15m'},
                self.risk_manager, self.conn, self.agent_state,
                send_alert=send_alert,
                notify_trade_entry=notify_trade_entry,
                add_position=self.risk_manager.add_position,
            )
