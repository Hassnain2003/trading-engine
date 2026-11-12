"""Central configuration for the execution engine.

All module-level constants, strategy registry, and timeframe mapping live here.
Values can be overridden via environment variables (see engine.py imports).

Active strategies: S2 (BTC) and S7 (BTC+ETH). S1, S3, S4, S5 are not in
ACTIVE_STRATEGIES (disabled after walk-forward showed no edge — see the
"Research honesty" section of the README). S9 (sacred harmonic) has been
removed from the public copy; ask the maintainer if you need it restored.
"""

import os
from utils.config_loader import get as _cfg

# ── Venue: Bybit demo (sandbox) only by default ──────────────────────────
# The engine connects to Bybit's demo/sandbox venue and nothing else.
BYBIT_DEMO = os.getenv('BYBIT_DEMO', 'true').lower() in ('1', 'true', 'yes', 'on')

# Mainnet is NEVER the default. Walking onto mainnet requires BOTH
# ALLOW_MAINNET=true AND I_UNDERSTAND_THE_RISK=yes (see execution/engine.py).
ALLOW_MAINNET = os.getenv('ALLOW_MAINNET', '').lower() in ('1', 'true', 'yes', 'on')
I_UNDERSTAND_THE_RISK = os.getenv('I_UNDERSTAND_THE_RISK', '').lower() == 'yes'

# New orders are placed only when explicitly opted in. Defaults to off so a
# fresh clone can never place an order unintentionally.
ENABLE_TRADING = os.getenv('ENABLE_TRADING', '').lower() in ('1', 'true', 'yes', 'on')


def mainnet_consent_required() -> tuple:
    """Return (must_refuse: bool, reason: str).

    Mainnet startup is refused unless BOTH ALLOW_MAINNET=true and
    I_UNDERSTAND_THE_RISK=yes are set, so the engine can never drift onto
    real-money trading by accident or by a single misconfigured flag.
    """
    if BYBIT_DEMO:
        return False, ''
    if ALLOW_MAINNET and I_UNDERSTAND_THE_RISK:
        return False, ''
    return True, (
        "Refusing to start on Bybit MAINNET. Mainnet trading is disabled by "
        "default. To opt in you must set BOTH ALLOW_MAINNET=true AND "
        "I_UNDERSTAND_THE_RISK=yes in your environment, and you must "
        "understand you can lose real money. Demo/sandbox (BYBIT_DEMO=true) "
        "is the default and needs no extra flags."
    )


# ── Strategy registry ────────────────────────────────────────────────────────
# (module_name, [symbols]) — single source of truth for what the engine scans.
# utils.stats.live_config_scope() reads STRATEGY_CONFIG + ACTIVE_STRATEGIES.
STRATEGY_CONFIG = [
    # S1, S3, S4, S5 registered but NOT active (no edge on walk-forward).
    ('strategy1_rsi_reversion',    ["BTC/USDT:USDT", "XRP/USDT:USDT"]),
    # SOL dropped 2026-07-19; BNB paused 2026-08-02 (stop overruns / net red).
    ('strategy2_breakout_retest',   ["BTC/USDT:USDT"]),
    ('strategy3_vwap_bands',        ["XRP/USDT:USDT", "ETH/USDT:USDT"]),
    ('strategy4_rsi_macd',          ["BTC/USDT:USDT", "ETH/USDT:USDT", "LINK/USDT:USDT", "DOGE/USDT:USDT"]),
    ('strategy5_ma_crossover',      ["DOGE/USDT:USDT", "BNB/USDT:USDT", "SUI/USDT:USDT"]),
    # 2026-08-25: cut to BTC+ETH. Per-coin live record showed BNB/XRP/DOGE are
    # the bleed; re-add an alt only after it clears the go-live gate on forward
    # data.
    ('strategy7_daily_momentum',    ["BTC/USDT:USDT", "ETH/USDT:USDT"]),
]

# S3 and S4 disabled 2026-07-08 after 6-month backtests showed no profitable
# configuration (S3: PF 0.35 baseline, best redesign 0.85; S4: PF 0.71-0.74).
ACTIVE_STRATEGIES = ['strategy2_breakout_retest', 'strategy7_daily_momentum']

STRATEGY_TIMEFRAME = {
    'strategy2_breakout_retest': '5m',
    'strategy3_vwap_bands': '5m',
    'strategy4_rsi_macd': '15m',
    # S7 is a daily-held position; scanning each 5m close is just the retry
    # cadence — _symbol_busy keeps it to one position per symbol and the
    # s7_manager only releases it on a daily direction flip.
    'strategy7_daily_momentum': '5m',
}

# Symbols untradeable on the DEMO venue (abandoned order books print fake
# prices that fire stops and produce garbage fills). Applied when loading
# strategy coin lists. Return when trading a real venue.
DEMO_EXCLUDED_SYMBOLS = {'SOL/USDT:USDT', 'LINK/USDT:USDT'}

BAR_REFERENCE_SYMBOL = 'BTC/USDT:USDT'

# ── Execution parameters (config.yaml + env override) ────────────────────────
DEFAULT_LEVERAGE = int(os.getenv('LEVERAGE', str(_cfg('execution', 'leverage', '10'))))
S3_LEVERAGE = int(os.getenv('S3_LEVERAGE', str(_cfg('execution', 's3_leverage', '15'))))
LIMIT_OFFSET_PCT = float(os.getenv('LIMIT_OFFSET_PCT', str(_cfg('execution', 'limit_offset_pct', '0.0002'))))
PENDING_ORDER_TTL_SEC = int(os.getenv('PENDING_ORDER_TTL_SEC', str(_cfg('execution', 'pending_order_ttl_sec', '600'))))
POLL_INTERVAL_SEC = int(os.getenv('POLL_INTERVAL_SEC', str(_cfg('execution', 'poll_interval_sec', '300'))))
IOC_MAX_CROSS_PCT = float(os.getenv('IOC_MAX_CROSS_PCT', str(_cfg('execution', 'ioc_max_cross_pct', '0.002'))))
CORRELATION_REFRESH_SEC = int(os.getenv('CORRELATION_REFRESH_SEC', str(_cfg('execution', 'correlation_refresh_sec', '3600'))))
MARKET_DATA_REFRESH_SEC = int(os.getenv('MARKET_DATA_REFRESH_SEC', str(_cfg('execution', 'market_data_refresh_sec', '300'))))
HEALTH_PORT = int(os.getenv('HEALTH_PORT', str(_cfg('execution', 'health_port', '8765'))))
STALE_BAR_MAX_AGE_SEC = int(os.getenv('STALE_BAR_MAX_AGE_SEC', str(_cfg('execution', 'stale_bar_max_age_sec', '600'))))  # 10 min for 5m bars
MAX_POSITION_AGE_SEC = int(os.getenv('MAX_POSITION_AGE_SEC', str(_cfg('execution', 'max_position_age_sec', '604800'))))  # 7 days default
MAX_ENTRIES_PER_STRATEGY_PER_DAY = int(os.getenv('MAX_ENTRIES_PER_STRATEGY_PER_DAY', str(_cfg('execution', 'max_entries_per_strategy_per_day', '3'))))

_BYBIT_COMMON = {
    'enableRateLimit': True,
    'timeout': 30000,
    'options': {'defaultType': 'linear'},
}