import logging
import os
import re
import time
from datetime import datetime, timezone, timedelta

logger = logging.getLogger('risk_manager')

from trading_phase import (
    DAILY_PROFIT_TARGET_PCT,
    FORCE_EQUITY,
    MICRO_STRATEGY_RISK_PCT,
    PHASE_ID,
    STARTING_EQUITY,
    phase_equity,
)
from claude.notifier import send_alert
from utils.config_loader import get as _cfg, load as _load_config
from execution.config import DEFAULT_LEVERAGE, S3_LEVERAGE

# ── Effective configuration (single source of truth) ─────────────────────────
# Precedence for the per-strategy risk dial, lowest → highest:
#   1. config.yaml [strategy_risk]        (base dials + default)
#   2. config.yaml [micro_strategy_risk]  ($500-phase overrides)
#   3. trading_phase.MICRO_STRATEGY_RISK_PCT (phase module constants)
#   4. SECTION__KEY env vars              (STRATEGY_RISK__X / MICRO_STRATEGY_RISK__X)
# Env is re-applied after step 3 because trading_phase values are Python
# constants, not config data — without the re-apply an env override would be
# silently clobbered by the phase dict. Notional caps come from config.yaml
# [notional_cap] only (env applied inside config_loader.load()).

_LABEL_RE = re.compile(r'^S(\d+)_')


def canonical_strategy_key(name):
    """Normalize a strategy identifier to its module form.

    Signal dicts carry label keys ('S7_Daily_Momentum') while config.yaml and
    the signals table carry module keys ('strategy7_daily_momentum'); both must
    resolve to the same dial.
    """
    if not isinstance(name, str):
        return name
    m = _LABEL_RE.match(name)
    if m:
        return f'strategy{m.group(1)}_{name[m.end():].lower()}'
    return name


def effective_strategy_risk() -> dict:
    """Merged per-strategy risk dials — see precedence comment above."""
    cfg = _load_config()
    eff = {}
    for src in (cfg.get('strategy_risk') or {},
                cfg.get('micro_strategy_risk') or {},
                MICRO_STRATEGY_RISK_PCT):
        for k, v in src.items():
            eff[canonical_strategy_key(k)] = float(v)
    # Re-apply env so SECTION__KEY overrides still beat the trading_phase dict.
    eff = _env_apply('strategy_risk', eff)
    eff = _env_apply('micro_strategy_risk', eff)
    return eff


def effective_notional_caps() -> dict:
    """Merged per-strategy notional caps from config.yaml [notional_cap]."""
    caps = {canonical_strategy_key(k): float(v)
            for k, v in (_load_config().get('notional_cap') or {}).items()}
    return _env_apply('notional_cap', caps)


def _env_apply(section: str, values: dict) -> dict:
    from utils.config_loader import apply_env_overrides
    return apply_env_overrides({section: values})[section]


# Hard ceiling for the $50 preservation window. Strategy dials may be lower;
# nothing may size above this, and the engine skips if that qty is below min lot.
MAX_TRADE_RISK_PCT = float(os.getenv('MAX_TRADE_RISK_PCT', str(_cfg('risk', 'max_trade_risk_pct', 0.02))))
DAILY_LOSS_LOCK_PCT = float(os.getenv('DAILY_LOSS_LOCK_PCT', '0.02'))   # pause new entries for the rest of the UTC day
DAILY_FLATTEN_PCT = float(os.getenv('DAILY_FLATTEN_PCT', '0.04'))       # market-close everything
WEEKLY_KILL_SWITCH_PCT = float(os.getenv('WEEKLY_KILL_SWITCH_PCT', '0.07'))  # 7% rolling 7-day drawdown -> halt all new entries


class RiskManager:
    # initial_balance defaults to 0.0 — the "not established yet" sentinel —
    # and deliberately NOT a plausible-looking number. A fabricated baseline
    # turns daily_pnl() into fiction: on 2026-08-26 a DNS blip failed the init
    # balance sync, which left session_start_equity at a hardcoded 10000 while
    # the real book was $51.08, so daily_pnl() read -9948.92, tripped the -4%
    # flatten, and blocked all entries for 24h on a network hiccup. With 0.0
    # the `start > 0` guards below keep both breakers disarmed until a real
    # equity figure arrives (venue read or today's local snapshot).
    def __init__(self, initial_balance=0.0):
        self.initial_balance = initial_balance
        self.current_balance = initial_balance
        self.session_start_equity = initial_balance
        # −2% session P&L: no new entries. −4% is a separate flatten in the engine.
        self.daily_loss_limit_pct = DAILY_LOSS_LOCK_PCT
        self.daily_flatten_pct = DAILY_FLATTEN_PCT
        # Soft daily profit lock: +2% / $1.00 at $50. Blocks NEW entries only —
        # open positions still managed to flip/SL/TP.
        self.daily_profit_target_pct = DAILY_PROFIT_TARGET_PCT
        # Per-strategy risk dials + notional caps — built once from config.yaml
        # with trading_phase and SECTION__KEY env overrides applied (2.1: single
        # source of truth; the old hardcoded STRATEGY_RISK_PCT dict and the
        # max_notional_pct_for_strategy if/elif chain are gone).
        self.strategy_risk = effective_strategy_risk()
        self.notional_caps = effective_notional_caps()
        self.position_risk_pct = self.strategy_risk.get('default', 0.01)
        self.leverage = DEFAULT_LEVERAGE
        # Nominal position-count ceilings (config.yaml risk.*). These are NOT
        # the effective limit: portfolio heat (6% of equity via stored
        # planned_risk_usd) binds first at $500 — see portfolio_heat_ok.
        self.max_positions = int(_cfg('risk', 'max_positions', 16))
        self.max_weekend_positions = int(_cfg('risk', 'max_weekend_positions', 14))
        self.correlation_cap_active = False
        self.open_positions = {}
        self.cooldown_until = None
        # --- Weekly kill switch (rolling 7-day drawdown) ---
        self.weekly_kill_switch_pct = WEEKLY_KILL_SWITCH_PCT
        self.weekly_killed = False
        self.weekly_killed_at = None
        self.weekly_high_water_mark = None   # equity peak used for 7-day DD calc
        self.weekly_high_water_ts = None     # timestamp of the peakne
        self.daily_target_hit = False
        self.daily_loss_locked = False
        self.daily_lock_alerted = False
        self.flattened_for_daily_loss = False
        self.last_signals = {}
        self._last_reset_date = None

    def _is_weekend(self):
        now = datetime.now(timezone.utc)
        return now.weekday() >= 5

    @property
    def equity_established(self):
        """True once a real equity figure has been observed for today, from the
        venue or from today's first local snapshot. While False the agent must
        neither open trades nor act on the daily-loss breakers — it does not
        know what its book is worth."""
        return (self.session_start_equity or 0) > 0

    def _reset_daily_if_new_day(self):
        today = datetime.now(timezone.utc).date()
        if self._last_reset_date is None:
            self._last_reset_date = today
            return
        if self._last_reset_date != today:
            self.session_start_equity = self.current_balance
            self.daily_target_hit = False
            self.daily_loss_locked = False
            self.daily_lock_alerted = False
            self.flattened_for_daily_loss = False
            self.cooldown_until = None
            self._last_reset_date = today

    def daily_pnl(self):
        # No baseline, or no balance observed yet => no opinion on P&L.
        # Differencing against a placeholder in EITHER direction is exactly how
        # the 2026-08-26 phantom -9948.92 flatten happened.
        if not self.equity_established or (self.current_balance or 0) <= 0:
            return 0.0
        return self.current_balance - self.session_start_equity

    def daily_target_usdt(self):
        return self.session_start_equity * self.daily_profit_target_pct

    def update_balance(self, new_balance, unrealized=0.0):
        """Return 'flatten', 'lock', or None.

        Uses wallet + mark-to-market so an open stop running against the book
        trips the daily lock before the close.
        """
        self.current_balance = phase_equity(new_balance) + float(unrealized or 0)
        self._reset_daily_if_new_day()
        if not self.equity_established:
            # First real equity we've seen with nothing to anchor to (venue was
            # down at startup and there was no snapshot for today). Adopt it as
            # today's baseline instead of measuring against a placeholder, and
            # fire no breaker on the establishing cycle — P&L against yourself
            # is zero by definition.
            self.session_start_equity = self.current_balance
            return None
        daily_pnl = self.daily_pnl()
        start = self.session_start_equity or 0.0
        if start > 0 and daily_pnl <= -start * self.daily_flatten_pct:
            self.daily_loss_locked = True
            return 'flatten'
        if start > 0 and daily_pnl <= -start * self.daily_loss_limit_pct:
            self.daily_loss_locked = True
            return 'lock'
        if daily_pnl >= self.daily_target_usdt() > 0:
            self.daily_target_hit = True
        return None

    def _position_cap(self):
        cap = self.max_weekend_positions if self._is_weekend() else self.max_positions
        if self.correlation_cap_active:
            cap = min(cap, 2)
        return cap

    def check_weekly_kill_switch(self, equity: float, now: datetime | None = None) -> bool:
        """Rolling 7-day drawdown kill switch.

        Tracks the equity high-water mark over the last 7 days. If current
        equity falls more than WEEKLY_KILL_SWITCH_PCT below that peak, all new
        entries are halted until the next UTC week. Returns True if the kill
        switch is currently armed (blocking entries).
        """
        now = now or datetime.now(timezone.utc)
        today = now.date()

        # Reset the high-water mark at the start of each UTC week (Monday).
        if self.weekly_high_water_ts is None or (
            self.weekly_high_water_ts.date() + timedelta(days=7) <= today
        ):
            self.weekly_high_water_mark = equity
            self.weekly_high_water_ts = now
            self.weekly_killed = False
            self.weekly_killed_at = None

        # Update high-water mark if we've set a new peak this week.
        if self.weekly_high_water_mark is None or equity > self.weekly_high_water_mark:
            self.weekly_high_water_mark = equity
            self.weekly_high_water_ts = now

        # Check drawdown against the 7-day rolling peak.
        if self.weekly_high_water_mark and self.weekly_high_water_mark > 0:
            dd = (self.weekly_high_water_mark - equity) / self.weekly_high_water_mark
            if dd >= self.weekly_kill_switch_pct and not self.weekly_killed:
                self.weekly_killed = True
                self.weekly_killed_at = now
                msg = (
                    f"🛑 *Weekly Kill Switch Armed*\n"
                    f"7-day drawdown {dd*100:.2f}% >= {self.weekly_kill_switch_pct*100:.0f}% threshold.\n"
                    f"Peak: ${self.weekly_high_water_mark:.2f} | Current: ${equity:.2f}\n"
                    f"New entries paused until next UTC week.\n"
                    f"Open positions still run to SL/TP."
                )
                logger.warning(msg)
                send_alert(msg)

        return self.weekly_killed

    def can_open_trade(self):
        self._reset_daily_if_new_day()
        # Fail closed: never size a position off an unknown book. If the venue
        # was unreachable at startup and no local snapshot anchored today, the
        # agent waits rather than trading blind (2026-08-26).
        if not self.equity_established:
            return False
        if self.cooldown_until and datetime.now(timezone.utc) < self.cooldown_until:
            return False
        if self.daily_loss_locked:
            return False
        if self.daily_target_hit:
            return False
        if self.weekly_killed:
            return False
        return len(self.open_positions) < self._position_cap()

    def risk_pct_for_strategy(self, strategy=None):
        # Effective dial from config.yaml + phase + env (see module header).
        # Canonicalized so 'S2_Breakout_Retest' and 'strategy2_breakout_retest'
        # resolve to the same value; unknown strategies get the config default.
        if not strategy:
            base = self.position_risk_pct
        else:
            base = self.strategy_risk.get(canonical_strategy_key(strategy),
                                          self.position_risk_pct)
        return min(base, MAX_TRADE_RISK_PCT)

    def effective_config(self) -> dict:
        """Flat, stable dict of the config that actually governs this instance.

        This is THE artifact for startup logging and config_hash (2.1): every
        signal's config_hash is derived from exactly this, so a dial change in
        config.yaml / trading_phase / env produces a different hash.
        Deterministic: sorted keys, plain scalars only (no memory addresses),
        so identical configuration hashes identically across interpreter runs.
        """
        return {
            'phase_id': PHASE_ID,
            'strategy_risk': {k: self.strategy_risk[k]
                              for k in sorted(self.strategy_risk)},
            'notional_cap': {k: self.notional_caps[k]
                             for k in sorted(self.notional_caps)},
            'leverage': int(self.leverage),
            'max_trade_risk_pct': MAX_TRADE_RISK_PCT,
            'daily_loss_lock_pct': self.daily_loss_limit_pct,
            'daily_flatten_pct': self.daily_flatten_pct,
            'daily_profit_target_pct': self.daily_profit_target_pct,
            'weekly_kill_switch_pct': self.weekly_kill_switch_pct,
            'max_positions': self.max_positions,
            'max_weekend_positions': self.max_weekend_positions,
            'portfolio_risk_cap_pct': self.MAX_PORTFOLIO_RISK_PCT,
            'correlated_risk_cap_pct': self.MAX_CORRELATED_RISK_PCT,
            'corr_entry_threshold': self.CORR_ENTRY_THRESHOLD,
        }

    def describe_effective_config(self) -> str:
        """Human-readable startup summary of effective_config() (2.1)."""
        lines = [f"Effective risk config [{PHASE_ID}] "
                 f"(config.yaml + trading_phase + env overrides):"]
        active = sorted(k for k in self.strategy_risk if k != 'default')
        for key in active:
            pct = self.strategy_risk[key]
            cap = self.notional_caps.get(key,
                                         self.notional_caps.get('default', 0.10))
            lines.append(f"  {key:<32} risk {pct*100:>5.2f}%  "
                         f"notional cap {cap*100:>4.0f}%")
        lines.append(f"  {'(default)':<32} risk "
                     f"{self.strategy_risk.get('default', 0.01)*100:>5.2f}%  "
                     f"notional cap "
                     f"{self.notional_caps.get('default', 0.10)*100:>4.0f}%")
        lines.append(
            f"  leverage {self.leverage}x | per-trade hard cap "
            f"{MAX_TRADE_RISK_PCT*100:.0f}% | portfolio heat "
            f"{self.MAX_PORTFOLIO_RISK_PCT*100:.0f}% | positions "
            f"{self.max_positions}/{self.max_weekend_positions} (weekend) | "
            f"correlation cap {'ON' if self.correlation_cap_active else 'off'}"
        )
        return "\n".join(lines)

    def drawdown_size_multiplier(self):
        """Return a position-size multiplier (0.0-1.0) based on daily drawdown.

        At 0% drawdown: full size (1.0).
        At -1% drawdown (halfway to -2% lock): half size (0.5).
        At -2% drawdown (lock): no new entries (0.0) — can_open_trade() already
        blocks at this point, but the multiplier enforces the taper anyway.
        """
        if not self.equity_established or self.session_start_equity <= 0:
            return 1.0
        daily_pnl_pct = self.daily_pnl() / self.session_start_equity
        if daily_pnl_pct >= 0:
            return 1.0
        lock_pct = self.daily_loss_limit_pct  # 0.02 → -2%
        if daily_pnl_pct <= -lock_pct:
            return 0.0
        return max(0.0, 1.0 + daily_pnl_pct / lock_pct)

    def max_notional_pct_for_strategy(self, strategy=None):
        # Caps must scale with the risk dial or they silently neuter it:
        # e.g. S7 at 1% risk with a ~6% ATR stop wants ~17% notional — a 10%
        # cap would quietly cut its true risk to 0.6%. S2's tight stops make
        # its true risk structurally cap-bound regardless (25% cap × ~1%
        # stop ≈ 0.25-0.4% real risk — raising it further would mean insane
        # single-coin concentration, so this is S2's honest ceiling).
        # Source: config.yaml [notional_cap] + SECTION__KEY env (2.1).
        if not strategy:
            return self.notional_caps.get('default', 0.10)
        return self.notional_caps.get(canonical_strategy_key(strategy),
                                      self.notional_caps.get('default', 0.10))

    def max_risk_usdt(self):
        return max(0.0, float(self.current_balance or 0) * MAX_TRADE_RISK_PCT)

    def calculate_position_size(self, entry_price, stop_loss, side, strategy=None):
        risk_pct = self.risk_pct_for_strategy(strategy)
        equity = float(self.current_balance or 0)
        if equity <= 0 or not entry_price:
            return 0.0
        risk_per_trade = min(equity * risk_pct, self.max_risk_usdt())

        # ── M4: Drawdown-based position reduction ──
        # Taper size as daily drawdown approaches the -2% lock.
        size_mult = self.drawdown_size_multiplier()
        if size_mult <= 0:
            return 0.0
        risk_per_trade *= size_mult

        min_stop_pct = 0.005   # minimum 0.5% stop distance
        min_stop_abs = entry_price * min_stop_pct

        if side == 'long':
            risk_per_unit = max(abs(entry_price - stop_loss), min_stop_abs)
        else:
            risk_per_unit = max(abs(stop_loss - entry_price), min_stop_abs)

        if risk_per_unit <= 0:
            return 0.0

        raw_qty = risk_per_trade / risk_per_unit

        max_notional = equity * self.max_notional_pct_for_strategy(strategy)
        notional = raw_qty * entry_price
        if notional > max_notional:
            raw_qty = max_notional / entry_price

        dollar_risk = raw_qty * risk_per_unit
        cap = self.max_risk_usdt()
        if dollar_risk > cap > 0:
            raw_qty = cap / risk_per_unit
        if raw_qty * risk_per_unit > cap + 1e-9:
            return 0.0
        return raw_qty

    def apply_regime_size_multiplier(self, adx_value):
        return 0.5 if adx_value > 40 else 1.0

    # ── Portfolio heat (correlation-aware circuit breaker) ────────────────
    # Limits aggregate risk across ALL open positions and across CLUSTERED
    # (highly correlated) positions. Sits in front of the per-trade cap:
    # each trade may be individually fine but the portfolio can still be
    # over-exposed when 3 correlated BTC-long positions all stop out together.

    # Max total $ risk across all open positions, as % of equity.
    MAX_PORTFOLIO_RISK_PCT = float(os.getenv('MAX_PORTFOLIO_RISK_PCT', '0.06'))
    # Max $ risk across positions correlated > CORR_ENTRY_THRESHOLD with the
    # candidate, as % of equity.
    MAX_CORRELATED_RISK_PCT = float(os.getenv('MAX_CORRELATED_RISK_PCT', '0.03'))
    # Correlation coefficient above which two symbols count as "clustered".
    CORR_ENTRY_THRESHOLD = 0.7

    def portfolio_heat_ok(self, new_symbol, new_side, new_strategy=None,
                          correlation_matrix=None, new_planned_risk_usd=None):
        """Gate a candidate entry against aggregate + correlated risk caps.

        Returns (ok: bool, reason: str). reason is '' when ok.
        Falls back to pass-through if equity is not yet established or no
        positions are open (fast path).

        Risk model (2.3): open positions contribute their STORED
        planned_risk_usd — the actual qty × stop distance recorded at entry —
        not equity × nominal dial. Positions without a stored value (legacy
        rows, adopted orphans) fall back to the nominal reconstruction.
        The candidate contributes new_planned_risk_usd when the caller has
        sized it (actual stop risk), else the nominal dial estimate.
        """
        equity = float(self.current_balance or 0)
        if equity <= 0:
            return True, ''   # breakers disarmed until real equity arrives

        open_pos = self.open_positions
        if not open_pos:
            return True, ''

        def _pos_risk(pos):
            stored = pos.get('planned_risk_usd')
            if stored is not None:
                try:
                    stored = float(stored)
                    if stored > 0:
                        return stored
                except (TypeError, ValueError):
                    pass
            # Fallback: reconstruct from the strategy dial (pre-2.3 behavior).
            strat = pos.get('strategy')
            pct = self.risk_pct_for_strategy(strat) if strat else self.position_risk_pct
            return equity * pct

        total_risk = sum(_pos_risk(p) for p in open_pos.values())
        if new_planned_risk_usd is not None:
            new_risk = max(0.0, float(new_planned_risk_usd))
        else:
            new_pct = self.risk_pct_for_strategy(new_strategy) if new_strategy else self.position_risk_pct
            new_risk = equity * new_pct

        # 1) Portfolio total cap
        if (total_risk + new_risk) > equity * self.MAX_PORTFOLIO_RISK_PCT:
            total_pct = (total_risk + new_risk) / equity
            return False, (
                f"portfolio heat {total_pct:.1%} > cap {self.MAX_PORTFOLIO_RISK_PCT:.0%} "
                f"({len(open_pos)} open + {new_symbol})"
            )

        # 2) Correlated-cluster cap (needs a matrix)
        if correlation_matrix:
            corr_risk = 0.0
            for sym, pos in open_pos.items():
                if sym == new_symbol:
                    continue
                corr = correlation_matrix.get(new_symbol, {}).get(sym)
                if corr is None:
                    corr = correlation_matrix.get(sym, {}).get(new_symbol)
                if corr is not None and abs(corr) >= self.CORR_ENTRY_THRESHOLD:
                    corr_risk += _pos_risk(pos)
            if (corr_risk + new_risk) > equity * self.MAX_CORRELATED_RISK_PCT:
                clust_pct = (corr_risk + new_risk) / equity
                return False, (
                    f"correlated cluster heat {clust_pct:.1%} > cap "
                    f"{self.MAX_CORRELATED_RISK_PCT:.0%} ({new_symbol})"
                )

        return True, ''

    # ── M6: Correlation-aware size scaling ─────────────────────────────────
    # Instead of binary block, gradually taper position size as correlation
    # with open positions rises. Returns 1.0 (full size) when no correlation,
    # tapering to 0.5 at CORR_ENTRY_THRESHOLD and below.
    def correlation_size_multiplier(self, symbol, correlation_matrix):
        """Return a size multiplier (0.5-1.0) based on max correlation with open positions.

        If no open positions or no matrix, returns 1.0 (no taper).
        If max |corr| with any open position >= CORR_ENTRY_THRESHOLD, returns 0.5.
        Otherwise interpolates linearly between 1.0 and 0.5.
        """
        if not self.open_positions or correlation_matrix is None:
            return 1.0

        max_corr = 0.0
        for other_sym in self.open_positions:
            if other_sym == symbol:
                continue
            corr = correlation_matrix.get(symbol, {}).get(other_sym)
            if corr is None:
                corr = correlation_matrix.get(other_sym, {}).get(symbol)
            if corr is not None:
                max_corr = max(max_corr, abs(corr))

        if max_corr <= 0:
            return 1.0
        if max_corr >= self.CORR_ENTRY_THRESHOLD:
            return 0.5
        # Linear taper: at CORR_ENTRY_THRESHOLD/2 → 0.75, at threshold → 0.5
        return 1.0 - 0.5 * (max_corr / self.CORR_ENTRY_THRESHOLD)

    def check_correlation_override(self, signal):
        now = datetime.now(timezone.utc)
        symbol = signal['symbol']
        side = signal['side']
        for other_symbol, (last_time, last_side) in self.last_signals.items():
            if other_symbol == symbol:
                continue
            if last_side == side and (now - last_time).total_seconds() < 900:
                return True
        self.last_signals[symbol] = (now, side)
        return False

    def add_position(self, symbol, side, entry, stop, target, qty, order_id,
                     strategy=None, planned_risk_usd=None):
        """Register an open position. planned_risk_usd (2.3) is the actual
        stop risk in USDT (qty × stop distance) recorded at entry — portfolio
        heat sums these instead of nominal equity × dial. None for legacy or
        adopted rows; heat falls back to the nominal dial for those."""
        self.open_positions[symbol] = {
            'side': side,
            'entry': entry,
            'stop': stop,
            'target': target,
            'quantity': qty,
            'order_id': order_id,
            'strategy': strategy,
            'entry_time': datetime.now(timezone.utc),
            'planned_risk_usd': planned_risk_usd,
        }

    def remove_position(self, symbol):
        self.open_positions.pop(symbol, None)
