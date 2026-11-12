"""Active trading phase — neutral demo defaults for the public copy.

All real values (phase id/label, starting equity, dates) come from
environment variables so no real balance, phase label, or account figure is
baked into the repository. Defaults are deliberately safe: STARTING_EQUITY
defaults to 0.0, which keeps the risk breakers disarmed until a real equity
figure is observed (the engine neither trades nor acts on daily-loss breakers
until equity is established).

Set these in .env (never committed) before running against a venue:

    PHASE_ID=demo_phase
    PHASE_LABEL="Demo phase — paper trading"
    STARTING_EQUITY=0.0
    FORCE_EQUITY=0
"""

import os
from datetime import datetime, timezone

from dotenv import load_dotenv

load_dotenv()

# Neutral phase identity — overridable via env. Never a real account label.
PHASE_ID = os.getenv('PHASE_ID', 'demo_phase').strip() or 'demo_phase'
PHASE_LABEL = os.getenv('PHASE_LABEL', 'Demo phase — paper trading').strip() or 'Demo phase — paper trading'
PHASE_STARTED_UTC = os.getenv('PHASE_STARTED_UTC', '2026-01-01T00:00:00+00:00').strip()

# Starting equity for sizing/daily-target. Defaults to 0.0 (not established)
# so the engine stays in a fail-closed state until a real balance lands.
STARTING_EQUITY = float(os.getenv('STARTING_EQUITY', '0') or 0.0)

# Each phase writes to its own SQLite journal so trade history is isolated.
# E.g. demo_phase → agent_trades_demo_phase.db
DB_FILENAME = f"agent_trades_{PHASE_ID}.db"
FORCE_EQUITY = os.getenv("FORCE_EQUITY", "0").strip() not in ("0", "false", "False")

# Daily profit target as a fraction of session equity (env-overridable).
DAILY_PROFIT_TARGET_PCT = float(os.getenv('DAILY_PROFIT_TARGET_PCT', '0.02'))

# Daily lock: blocks NEW entries only when hit; open positions still run to
# SL/TP/flip. Not a quota to chase.
DAILY_PROFIT_FLOOR_USDT = None  # computed as equity * DAILY_PROFIT_TARGET_PCT

# Per-strategy risk dial (fraction of equity), env-overridable. Conservative
# defaults; the public copy ships S2 and S7 only. Both module-form and
# label-form keys are accepted (risk_manager.canonical_strategy_key normalizes).
MICRO_STRATEGY_RISK_PCT = {
    'strategy2_breakout_retest': float(os.getenv('MICRO_STRATEGY_RISK__STRATEGY2_BREAKOUT_RETEST', '0.015')),
    'S2_Breakout_Retest': float(os.getenv('MICRO_STRATEGY_RISK__S2_BREAKOUT_RETEST', '0.015')),
    'strategy7_daily_momentum': float(os.getenv('MICRO_STRATEGY_RISK__STRATEGY7_DAILY_MOMENTUM', '0.008')),
    'S7_Daily_Momentum': float(os.getenv('MICRO_STRATEGY_RISK__S7_DAILY_MOMENTUM', '0.008')),
}
# Skip exchange min-lot bump when that lot's stop-loss dollars exceed this
# share of equity. Env-overridable.
MIN_LOT_MAX_STOP_RISK_PCT = float(os.getenv('MIN_LOT_MAX_STOP_RISK_PCT', '0.02'))

# Filled at runtime from kv_state / first balance sync when FORCE_EQUITY.
_wallet_baseline = None


def set_wallet_baseline(wallet_usdt):
    global _wallet_baseline
    if wallet_usdt is None:
        return
    _wallet_baseline = float(wallet_usdt)


def get_wallet_baseline():
    return _wallet_baseline


def phase_equity(wallet_usdt):
    """Equity for sizing, daily target, snapshots, and KPIs."""
    if wallet_usdt is None:
        return float(STARTING_EQUITY)
    w = float(wallet_usdt)
    if not FORCE_EQUITY:
        return w
    base = _wallet_baseline if _wallet_baseline is not None else w
    return float(STARTING_EQUITY) + (w - base)


# Back-compat alias used by earlier edits
effective_equity = phase_equity


def daily_profit_target_usdt(equity):
    return float(equity) * DAILY_PROFIT_TARGET_PCT


def daily_profit_floor_usdt(equity):
    """Daily target expressed in USDT for the given equity."""
    return daily_profit_target_usdt(equity)


def phase_started_dt():
    return datetime.fromisoformat(PHASE_STARTED_UTC)
