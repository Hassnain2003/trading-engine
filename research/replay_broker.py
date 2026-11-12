"""ReplayBroker — execution broker for the Stage 3 walk-forward harness.

Reproduces live Bybit execution semantics (SEMANTICS.md §1-§8) using local
candle data. Drives the ReplayExchange and the live RiskManager through one
closed 5m bar at a time, producing a complete trade ledger.

Per-bar order of operations (matching execution/engine.py):
  1. Funding (at 00:00/08:00/16:00 UTC, before exit checks)            R8
  2. Exchange-hosted SL/TP against this bar's OHLC (intrabar path)    R1-R3
     3. Management: daily risk lock, weekly kill, trailing stop,         R9/R4
     S7 flip, S7 soft-loss, max position age                            R6/R6b/R8
  4. Signal-reversal exit                                             R5
  5. Entry scan (STRATEGY_CONFIG order, one owner per symbol)         R11
"""

from __future__ import annotations

import importlib
import math
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from typing import Optional

from execution.config import (
    ACTIVE_STRATEGIES, STRATEGY_CONFIG, MAX_ENTRIES_PER_STRATEGY_PER_DAY,
)
from execution.entry_scanner import (
    check_liquidity, ensure_min_exchange_qty, symbol_busy,
)
from execution.position_sync import normalize_side
from risk.risk_manager import RiskManager
from research.replay_exchange import ReplayExchange, patched_now, strategy_modules

# ─── Cost constants ─────────────────────────────────────────────────────
FEE_BPS = 0.55          # Bybit taker fee per side: 5.5 bps = 0.055 %
FUNDING_HOURS = (0, 8, 16)

# ─── Execution constants (live code values) ─────────────────────────────
MAX_POSITION_AGE_SEC = 7 * 24 * 3600   # execution/config.py:70
SOFT_LOSS_PCT = 0.025                  # strategy7: SOFT_LOSS_PCT
TRAIL_PCT = {                          # execution/position_manager.py:142-146
    'strategy2_breakout_retest': 0.015,
    'strategy7_daily_momentum': 0.020,
}
MIN_STOP_PCT = 0.005                   # risk/risk_manager.py:361 (hard-coded)


@dataclass
class Position:
    symbol: str
    side: str
    entry: float            # signal entry price
    entry_fill: float       # actual fill price (after entry slippage)
    qty: float
    stop_loss: float        # original stop
    take_profit: float      # original target
    current_sl: float       # trailing-adjusted stop
    strategy: str
    entry_time: datetime
    entry_ts_ms: int
    planned_risk_usd: float
    entry_fee: float = 0.0
    funding_paid: float = 0.0  # cumulative signed funding while open


@dataclass
class Trade:
    symbol: str
    side: str
    entry: float
    entry_fill: float
    exit_trigger: float     # SL/TP price that triggered (or None)
    exit_fill: float        # actual exit fill price
    qty: float
    pnl_gross: float
    entry_fee: float
    exit_fee: float
    funding_total: float
    pnl_net: float
    exit_reason: str
    entry_time: datetime
    exit_time: datetime
    strategy: str
    r_multiple: Optional[float] = None


@dataclass
class FundingPayment:
    symbol: str
    side: str
    qty: float
    notional: float
    rate: float
    amount: float           # signed: negative = paid by account
    timestamp: datetime


@dataclass
class Event:
    ts: datetime
    type: str               # 'fill', 'exit', 'funding', 'risk_flat', ...
    symbol: str
    detail: dict = field(default_factory=dict)


class ReplayBroker:
    """Faithful replay of the live execution engine over local candle data."""

    def __init__(self, exchange: ReplayExchange, starting_equity: float = 500.0,
                 slippage_bps: float = 3.0, fee_bps: float = FEE_BPS,
                 cap_mode: str = 'code'):
        self.exchange = exchange
        self.starting_equity = starting_equity
        self.cash = starting_equity
        self.slippage = slippage_bps / 10000.0
        self.fee_rate = fee_bps / 10000.0
        self.cap_mode = cap_mode  # 'code' (literal code semantics) or 'intent'

        # Live risk manager — same code path as production
        self.risk_manager = RiskManager(initial_balance=starting_equity)
        self.risk_manager.current_balance = starting_equity
        self.risk_manager.session_start_equity = starting_equity
        # equity_established is read-only (session_start_equity > 0 → True)

        # Position book
        self.positions: dict[str, Position] = {}
        self.trades: list[Trade] = []
        self.funding_payments: list[FundingPayment] = []
        self.equity_snapshots: list[tuple[datetime, float, float]] = []
        self.events: list[Event] = []

        # Entry-cap state (mirrors entry_scanner.scan_entries)
        self.agent_state = {
            '_entry_cap_date': None,
            '_entries_per_strategy': {},
            'paused': False,
        }

        # Weekly kill-switch state
        self._weekly_hwm = None
        self._weekly_hwm_ts = None
        self._weekly_killed = False

        # Load live strategies (same registry as execution/engine.py)
        self.strategies = self._load_strategies()
        self._strategy_modules = strategy_modules()

    # ── strategy loading ───────────────────────────────────────────────
    def _load_strategies(self):
        func_map = {
            'strategy2_breakout_retest': 'strategy2_signal',
            'strategy7_daily_momentum': 'strategy7_signal',
        }
        out = []
        for mod_name, coins in STRATEGY_CONFIG:
            if mod_name not in ACTIVE_STRATEGIES:
                continue
            mod = importlib.import_module(f'strategies.{mod_name}')
            func = getattr(mod, func_map[mod_name])
            out.append((mod_name, func, list(coins)))
        return out

    # ── helpers ────────────────────────────────────────────────────────
    def _is_funding_ts(self, ts: datetime) -> bool:
        return ts.hour in FUNDING_HOURS and ts.minute == 0 and ts.second == 0

    def _is_new_utc_day(self, ts: datetime) -> bool:
        return ts.hour == 0 and ts.minute == 0 and ts.second == 0

    def _apply_slippage(self, side: str, price: float, is_exit: bool) -> float:
        """Entry: long fills at price*(1+s), short at price*(1-s).
        Exit: long fills at price*(1-s), short at price*(1+s)."""
        if is_exit:
            mult = (1.0 - self.slippage) if side == 'long' else (1.0 + self.slippage)
        else:
            mult = (1.0 + self.slippage) if side == 'long' else (1.0 - self.slippage)
        return price * mult

    def _fee(self, qty: float, price: float) -> float:
        return abs(qty) * price * self.fee_rate

    def _unrealized(self, close_prices: dict) -> float:
        total = 0.0
        for sym, pos in self.positions.items():
            px = close_prices.get(sym, pos.entry_fill)
            if pos.side == 'long':
                total += pos.qty * (px - pos.entry_fill)
            else:
                total += pos.qty * (pos.entry_fill - px)
        return total

    def _equity(self, close_prices: dict) -> float:
        return self.cash + self._unrealized(close_prices)

    # ── funding (R8) ───────────────────────────────────────────────────
    def _apply_funding(self, ts: datetime, mark_price: float):
        """Apply funding to all positions open at the funding instant.

        Bybit settles every 8h at 00:00/08:00/16:00 UTC. A long pays notional×rate
        when rate > 0; a short receives it. The funding_payments.amount column is
        signed from the account's perspective (negative = paid), matching
        position_sync.py:84 (net = gross - entry_fee - exit_fee + funding_val).
        """
        from research.data_loader import load_funding
        for sym, pos in self.positions.items():
            try:
                fdf = load_funding(sym)
            except Exception:
                continue
            row = fdf[fdf.index == ts]
            if row.empty:
                continue
            rate = float(row['funding_rate'].iloc[0])
            notional = pos.qty * mark_price
            amount = -notional * rate if pos.side == 'long' else notional * rate
            pos.funding_paid += amount
            self.cash += amount
            self.funding_payments.append(FundingPayment(
                symbol=sym, side=pos.side, qty=pos.qty, notional=notional,
                rate=rate, amount=amount, timestamp=ts,
            ))
            self.events.append(Event(ts, 'funding', sym, {
                'rate': rate, 'notional': notional, 'amount': amount,
            }))

    # ── SL/TP intrabar check (R1-R3) ──────────────────────────────────
    def _check_sl_tp(self, ts: datetime, bar: dict):
        """Check exchange-hosted SL/TP against this bar's OHLC.

        R3: compare open against trigger first (gap fill at open).
        R1: if both stop and target inside range, stop fills first.
        Fill price = trigger exactly, then exit slippage applied.
        """
        sym = bar.get('symbol')
        if sym is None:
            return
        pos = self.positions.get(sym)
        if pos is None:
            return
        o = bar['open']; h = bar['high']; l = bar['low']; c = bar['close']

        triggered = None  # ('stop'|'target', trigger_price)
        if pos.side == 'long':
            if o <= pos.current_sl:
                triggered = ('stop', pos.current_sl)                       # R3 gap
            elif h >= pos.take_profit and l <= pos.current_sl:
                triggered = ('stop', pos.current_sl)                       # R1: stop first
            elif h >= pos.take_profit:
                triggered = ('target', pos.take_profit)
            elif l <= pos.current_sl:
                triggered = ('stop', pos.current_sl)
        else:  # short
            if o >= pos.current_sl:
                triggered = ('stop', pos.current_sl)                       # R3 gap
            elif l <= pos.take_profit and h >= pos.current_sl:
                triggered = ('stop', pos.current_sl)                       # R1: stop first
            elif l <= pos.take_profit:
                triggered = ('target', pos.take_profit)
            elif h >= pos.current_sl:
                triggered = ('stop', pos.current_sl)

        if triggered is None:
            return

        kind, trigger_px = triggered
        exit_fill = self._apply_slippage(pos.side, trigger_px, is_exit=True)
        reason = 'stop_loss' if kind == 'stop' else 'take_profit'
        self._close_position(sym, exit_fill, ts, reason,
                             exit_trigger=trigger_px)

    # ── position close ─────────────────────────────────────────────────
    def _close_position(self, sym: str, exit_fill: float, ts: datetime,
                        exit_reason: str, exit_trigger: Optional[float] = None):
        if sym not in self.positions:
            return  # already closed by an earlier exit this bar
        pos = self.positions.pop(sym)
        if pos.side == 'long':
            pnl_gross = (exit_fill - pos.entry_fill) * pos.qty
        else:
            pnl_gross = (pos.entry_fill - exit_fill) * pos.qty

        exit_fee = self._fee(pos.qty, exit_fill)
        funding_total = pos.funding_paid
        pnl_net = pnl_gross - pos.entry_fee - exit_fee + funding_total

        # R-multiple: net P&L divided by planned risk in USDT (qty × stop dist)
        r_multiple = (pnl_net / pos.planned_risk_usd) if pos.planned_risk_usd and pos.planned_risk_usd > 0 else None

        self.trades.append(Trade(
            symbol=sym, side=pos.side, entry=pos.entry, entry_fill=pos.entry_fill,
            exit_trigger=exit_trigger if exit_trigger is not None else (
                pos.stop_loss if exit_reason == 'stop_loss' else pos.take_profit),
            exit_fill=exit_fill, qty=pos.qty, pnl_gross=pnl_gross,
            entry_fee=pos.entry_fee, exit_fee=exit_fee,
            funding_total=funding_total, pnl_net=pnl_net,
            exit_reason=exit_reason, entry_time=pos.entry_time,
            exit_time=ts, strategy=pos.strategy, r_multiple=r_multiple,
        ))

        self.cash += pnl_gross - exit_fee + funding_total
        self.risk_manager.remove_position(sym)

        self.events.append(Event(ts, 'exit', sym, {
            'exit_fill': exit_fill, 'pnl_net': pnl_net, 'reason': exit_reason,
            'strategy': pos.strategy, 'trigger': exit_trigger,
        }))

    # ── management: equity & daily risk locks (R9) ─────────────────────
    def _update_equity_and_daily_locks(self, ts: datetime, close_prices: dict):
        """Update RiskManager balance and apply daily -4%/-2%/+2% locks."""
        unrealized = self._unrealized(close_prices)
        self.risk_manager.current_balance = self.cash + unrealized
        action = self.risk_manager.update_balance(self.cash, unrealized=unrealized)

        if action == 'flatten':
            # Close all positions at bar close
            for sym in list(self.positions.keys()):
                pos = self.positions[sym]
                exit_px = close_prices.get(sym, pos.entry_fill)
                exit_fill = self._apply_slippage(pos.side, exit_px, is_exit=True)
                self._close_position(sym, exit_fill, ts, 'risk_flatten',
                                     exit_trigger=exit_px)
        # 'lock' just blocks new entries via can_open_trade(); no action needed here

    # ── weekly kill switch ─────────────────────────────────────────────
    def _check_weekly_kill(self, ts: datetime, equity: float):
        now = ts
        today = now.date()
        if self._weekly_hwm_ts is None or \
           (self._weekly_hwm_ts.date() + timedelta(days=7) <= today):
            self._weekly_hwm = equity
            self._weekly_hwm_ts = now
            self._weekly_killed = False
        if self._weekly_hwm is None or equity > self._weekly_hwm:
            self._weekly_hwm = equity
        if self._weekly_hwm and self._weekly_hwm > 0:
            dd = (self._weekly_hwm - equity) / self._weekly_hwm
            if dd >= self.risk_manager.weekly_kill_switch_pct and not self._weekly_killed:
                self._weekly_killed = True
        self.risk_manager.weekly_killed = self._weekly_killed

    # ── trailing stop (R4) ─────────────────────────────────────────────
    def _update_trailing_stops(self, ts: datetime, close_prices: dict):
        """Trail stop on bar close (mark = bar close), effective next bar.

        Never widens the stop: long → max(old, new); short → min(old, new).
        """
        for sym, pos in self.positions.items():
            strategy = pos.strategy
            if strategy not in TRAIL_PCT:
                continue
            side = normalize_side(pos.side)
            mark = close_prices.get(sym, pos.entry_fill)
            if mark <= 0:
                continue
            trail_pct = TRAIL_PCT[strategy]
            if side == 'long':
                new_sl = mark * (1.0 - trail_pct)
                if pos.current_sl is not None and new_sl <= pos.current_sl:
                    continue  # don't widen
            else:
                new_sl = mark * (1.0 + trail_pct)
                if pos.current_sl is not None and new_sl >= pos.current_sl:
                    continue  # don't widen
            pos.current_sl = new_sl
            pos.stop_loss = new_sl


            pos.current_sl = new_sl
            pos.stop_loss = new_sl

    # ── S7 flip (R6) & soft loss (R6b) ────────────────────────────────
    def _check_s7_flip(self, ts: datetime, bar: dict):
        """S7 day-flip at 00:00 UTC boundary using last completed daily bar.

        Fill at the 00:00 boundary price (open of first 5m bar of new day),
        which equals this bar's close when ts is exactly midnight.
        """
        from strategies.strategy7_daily_momentum import LONG_ONLY
        if not self._is_new_utc_day(ts):
            return

        for sym in list(self.positions.keys()):
            pos = self.positions.get(sym)
            if pos is None or pos.strategy != 'strategy7_daily_momentum':
                continue

            # Position must be held at least until next UTC day boundary
            entry_date = pos.entry_time.date()
            if entry_date >= ts.date():
                continue

            # Long-only: flatten shorts immediately
            if LONG_ONLY and normalize_side(pos.side) == 'short':
                exit_fill = self._apply_slippage(pos.side, bar['close'], is_exit=True)
                self._close_position(sym, exit_fill, ts, 's7_long_only_flatten_short')
                continue

            # Read last completed daily bar (daily[-2] at the 00:00 boundary)
            try:
                daily = self.exchange.fetch_ohlcv(sym, '1d', limit=3)
                if not daily or len(daily) < 2:
                    continue
                y = daily[-2]  # [ts_ms, open, high, low, close, volume]
                y_ret = (y[4] - y[1]) / y[1]
            except Exception:
                continue

            if y_ret == 0:
                continue

            desired = 'long' if y_ret > 0 else 'short'
            current = normalize_side(pos.side)
            if desired == current:
                continue  # keep

            # Long-only: flip to short -> close long, stay flat
            if LONG_ONLY and desired == 'short':
                exit_fill = self._apply_slippage(pos.side, bar['close'], is_exit=True)
                self._close_position(sym, exit_fill, ts,
                                     f's7_flip_prev_day_{y_ret*100:+.2f}%')
                continue

            exit_fill = self._apply_slippage(pos.side, bar['close'], is_exit=True)
            self._close_position(sym, exit_fill, ts,
                                 f's7_flip_prev_day_{y_ret*100:+.2f}%')

    def _check_s7_soft_loss(self, ts: datetime, close_prices: dict):
        """S7 soft-loss cap: pnl_pct <= -SOFT_LOSS_PCT -> close at bar close."""
        for sym in list(self.positions.keys()):
            pos = self.positions.get(sym)
            if pos is None or pos.strategy != 'strategy7_daily_momentum':
                continue
            mark = close_prices.get(sym, pos.entry_fill)
            if mark <= 0:
                continue
            if pos.side == 'long':
                pnl_pct = (mark - pos.entry_fill) / pos.entry_fill
            else:
                pnl_pct = (pos.entry_fill - mark) / pos.entry_fill
            if pnl_pct <= -SOFT_LOSS_PCT:
                exit_fill = self._apply_slippage(pos.side, mark, is_exit=True)
                self._close_position(sym, exit_fill, ts,
                                     f's7_soft_loss_{pnl_pct*100:.2f}%')

        # S7 soft-loss handled above; nothing S9-specific remains.

    # ── max position age ───────────────────────────────────────────────
    def _check_max_position_age(self, ts: datetime, close_prices: dict):
        now_ts = ts.timestamp()
        for sym in list(self.positions.keys()):
            pos = self.positions.get(sym)
            if pos is None:
                continue
            age = now_ts - pos.entry_ts_ms / 1000.0
            if age > MAX_POSITION_AGE_SEC:
                if sym not in self.positions:
                    continue  # already closed by an earlier exit this bar
                exit_fill = self._apply_slippage(
                    pos.side, close_prices.get(sym, pos.entry_fill), is_exit=True)
                self._close_position(sym, exit_fill, ts, 'max_position_age')
                self._close_position(sym, exit_fill, ts, 'max_position_age')

    # ── signal reversal (R5) ───────────────────────────────────────────
    def _check_signal_reversals(self, ts: datetime, close_prices: dict):
        """Close open positions when the live signal reverses direction.

        Reproduces entry_scanner.check_signal_reversal_exits including its
        last-writer-wins symbol->strategy mapping quirk (BTC positions are
        reversal-checked against S7's signal regardless of owner).
        """
        symbol_to_strategy = {}
        for mod_name, func, coins in self.strategies:
            for symbol in coins:
                symbol_to_strategy[symbol] = (mod_name, func)

        for sym, pos in list(self.positions.items()):
            if sym not in symbol_to_strategy:
                continue
            mod_name, func = symbol_to_strategy[sym]
            pos_side = normalize_side(pos.side)
            try:
                signal, _msg = func(self.exchange, sym)
            except Exception:
                continue
            if signal is None:
                continue
            sig_side = signal.get('side', 'long')
            if sig_side == pos_side:
                continue  # same direction — hold
            exit_fill = self._apply_slippage(
                pos.side, close_prices.get(sym, pos.entry_fill), is_exit=True)
            self._close_position(sym, exit_fill, ts, 'signal_reversal')
            self._close_position(sym, exit_fill, ts, 'signal_reversal')

    # ── entry scan ────────────────────────────────────────────────────
    def _scan_entries(self, ts: datetime, mark_price: float):
        """Run entry scan for all active strategies, mirroring scan_entries."""
        today = ts.strftime('%Y-%m-%d')
        if self.agent_state.get('_entry_cap_date') != today:
            self.agent_state['_entry_cap_date'] = today
            self.agent_state['_entries_per_strategy'] = {}
        entries_per_strategy = self.agent_state.setdefault('_entries_per_strategy', {})

        for mod_name, func, coins in self.strategies:
            if self.agent_state.get('paused'):
                return
            n_entries = entries_per_strategy.get(mod_name, 0)
            if n_entries >= MAX_ENTRIES_PER_STRATEGY_PER_DAY:
                continue

            for symbol in coins:
                n_entries = entries_per_strategy.get(mod_name, 0)
                if n_entries >= MAX_ENTRIES_PER_STRATEGY_PER_DAY:
                    break

                self._try_entry(mod_name, func, symbol, ts, mark_price)

                # Increment per live code semantics (per evaluation, not per entry)
                n_entries = entries_per_strategy.get(mod_name, 0)
                if n_entries < MAX_ENTRIES_PER_STRATEGY_PER_DAY:
                    entries_per_strategy[mod_name] = n_entries + 1

                # Increment per live code semantics (per evaluation, not per entry)
                n_entries = entries_per_strategy.get(mod_name, 0)
                if n_entries < MAX_ENTRIES_PER_STRATEGY_PER_DAY:
                    entries_per_strategy[mod_name] = n_entries + 1

    def _try_entry(self, mod_name: str, func, symbol: str, ts: datetime,
                   mark_price: float):
        """Evaluate one strategy/symbol and place an entry if conditions met.

        Mirrors execution/entry_scanner.try_entry exactly.
        """
        # Liquidity gate (spread=0 in replay; 1h notional from data) — D5
        gate_ok, _gate_msg = check_liquidity(self.exchange, symbol)
        if not gate_ok:
            return

        # Strategy signal (clock pinned to bar close ts)
        signal, _msg = func(self.exchange, symbol)
        if signal is None:
            return

        # Risk-manager gate
        if not self.risk_manager.can_open_trade():
            return

        # One owner per symbol (R11): in-memory positions
        if symbol in self.risk_manager.open_positions:
            return
        # symbol_busy also checks the DB for pending/open signals; in the
        # harness there is no DB, and open_positions is already checked above.
        if symbol in self.positions:
            return

        strategy_key = signal.get('strategy') or mod_name

        # Position sizing (live RiskManager)
        base_qty = self.risk_manager.calculate_position_size(
            signal['price'], signal['stop_loss'], signal['side'],
            strategy=strategy_key,
        )
        adx = signal.get('adx', 20)
        mult = self.risk_manager.apply_regime_size_multiplier(adx)
        corr_mult = self.risk_manager.correlation_size_multiplier(
            symbol, self.agent_state.get('correlation_matrix'),
        )
        mult *= corr_mult
        qty = base_qty * mult
        if qty <= 0:
            return

        # Min-lot bump (Bybit minimums from market_meta.json)
        bal = float(self.risk_manager.current_balance or 0)
        qty = ensure_min_exchange_qty(
            self.exchange, symbol, qty, signal['price'],
            stop_loss=signal.get('stop_loss'),
            balance=bal,
            max_notional_pct=self.risk_manager.max_notional_pct_for_strategy(
                strategy_key),
        )
        if qty <= 0:
            return

        # Portfolio heat on actual stop risk
        _sl = signal.get('stop_loss')
        candidate_risk = qty * abs(signal['price'] - _sl) if _sl else None
        heat_ok, _heat_reason = self.risk_manager.portfolio_heat_ok(
            symbol, signal['side'], new_strategy=strategy_key,
            correlation_matrix=self.agent_state.get('correlation_matrix'),
            new_planned_risk_usd=candidate_risk,
        )
        if not heat_ok:
            return

        # Fill at signal price +/- entry slippage (IOC taker)
        entry_fill = self._apply_slippage(signal['side'], signal['price'],
                                          is_exit=False)
        entry_fee = self._fee(qty, entry_fill)
        self.cash -= entry_fee

        planned_risk = qty * abs(signal['price'] - signal.get(
            'stop_loss', 0)) if signal.get('stop_loss') else None

        position = Position(
            symbol=symbol, side=signal['side'], entry=signal['price'],
            entry_fill=entry_fill, qty=qty,
            stop_loss=signal['stop_loss'], take_profit=signal['take_profit'],
            current_sl=signal['stop_loss'], strategy=strategy_key,
            entry_time=ts, entry_ts_ms=int(ts.timestamp() * 1000),
            planned_risk_usd=planned_risk or 0.0,
            entry_fee=entry_fee,
        )
        self.positions[symbol] = position
        self.risk_manager.add_position(
            symbol, signal['side'], signal['price'], signal['stop_loss'],
            signal['take_profit'], qty,
            f'replay_{symbol}_{int(ts.timestamp())}',
            strategy=strategy_key, planned_risk_usd=planned_risk,
        )

        self.events.append(Event(ts, 'fill', symbol, {
            'entry_fill': entry_fill, 'qty': qty, 'fee': entry_fee,
            'strategy': strategy_key, 'signal_price': signal['price'],
        }))

    # ── per-bar step ──────────────────────────────────────────────────
    def step(self, ts: datetime, bars_5m: dict):
        """Process one closed 5m bar.

        `bars_5m` maps symbol -> {'open','high','low','close','volume'} for
        the bar that just closed at `ts`. The reference symbol's bar provides
        the clock; all strategies are evaluated at this instant.
        """
        # Sync the exchange clock with the simulated bar close so that
        # fetch_ohlcv() serves only bars that existed at this instant.
        self.exchange.set_now(ts)

        close_prices = {sym: b['close'] for sym, b in bars_5m.items()}

        with patched_now(self._strategy_modules)(self._strategy_modules, ts):
            # 1. Funding (at funding timestamps, before exits) — R8
            if self._is_funding_ts(ts):
                ref_px = bars_5m.get('ETH/USDT:USDT',
                                   bars_5m[list(bars_5m.keys())[0]])['close']
                self._apply_funding(ts, ref_px)

            # 2. SL/TP intrabar check — R1-R3 (each symbol uses its own bar)
            for sym, bar in bars_5m.items():
                self._check_sl_tp(ts, {**bar, 'symbol': sym})

            # 3. Management phase
            self._update_equity_and_daily_locks(ts, close_prices)
            equity = self._equity(close_prices)
            self._check_weekly_kill(ts, equity)
            self._update_trailing_stops(ts, close_prices)
            ref_bar = bars_5m.get('ETH/USDT:USDT',
                                  bars_5m[list(bars_5m.keys())[0]])
            self._check_s7_flip(ts, ref_bar)
            self._check_s7_soft_loss(ts, close_prices)
            self._check_max_position_age(ts, close_prices)

            # 4. Signal reversal — R5
            self._check_signal_reversals(ts, close_prices)

            # 5. Entry scan — R11
            self._scan_entries(ts, mark_price=0.0)

        # 6. Record equity snapshot
        equity = self._equity(close_prices)
        self.equity_snapshots.append((ts, equity, self.cash))
