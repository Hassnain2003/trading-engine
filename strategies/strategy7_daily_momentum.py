"""S7 — Daily Momentum Continuation.

Rule (no fitted parameters): hold each symbol in the direction of its
previous UTC day's return. Validated in backtest/pattern_mining.py and
backtest/p1_daily_momentum_strategy.py: discovery PF 1.17 / validation PF
1.20 (Sharpe 1.27) after IOC costs, consistent across all 7 tested symbols
in both windows. Day-of-week and hour-of-day patterns tested alongside it
flipped sign out-of-sample and were rejected.

Execution model: the engine enters via this signal (IOC); the position is
held until the daily direction flips, at which point execution/s7_manager.py
closes it at the UTC day boundary and the next scan re-enters the other way.
SL/TP here are catastrophic guards sized off daily ATR, not the exit path —
wide enough that the time-based flip is the normal exit.
"""

import pandas as pd

try:
    import pandas_ta as ta
except ImportError:  # pandas_ta/numba unavailable on Python 3.14; real lib used on CI (Python 3.12).
    ta = None
from datetime import datetime, timezone

# NOTE: the LIVE coin list is engine.py STRATEGY_CONFIG, not this constant —
# the engine does not import COINS. Kept in sync for the standalone scanner
# and documentation only.
# 2026-08-25: consolidated to BTC+ETH. The 11-coin universe (added 2026-07-14
# from backtest/s7_universe_expansion.py, "val PF 1.14-1.45") was overfit and
# did not hold live: per-coin live record (long-only) had BTC +13.5% (57% WR)
# and ETH +3.6% (60% WR) positive, while the alts lost -51% combined (XRP
# -31.7%, plus BNB/SOL/DOGE/APT/NEAR/SUI all negative at 23% WR). The daily-
# momentum edge lives in the majors, matching S2 (BTC-only). Re-add an alt
# only after it clears the go-live gate on forward data.
COINS = ["BTC/USDT:USDT", "ETH/USDT:USDT"]

ATR_PERIOD = 14
ATR_SL_MULT = 2.0   # ~2x daily ATR: crash guard, rarely hit intraday
ATR_TP_MULT = 4.0   # far target; the daily flip is the real exit

BTC_SYMBOL = "BTC/USDT:USDT"
# 2026-08-02: shorts still toxic even with BTC-up filter (ETH short −7.2% Aug 1).
# Long-only until a down-regime sample proves shorts again.
LONG_ONLY = True
# Legacy flag kept for reference; LONG_ONLY supersedes it.
BLOCK_SHORTS_WHEN_BTC_UP = True
# Live data: UTC hour-0 cluster (day-flip re-entries) was ~-47% of S7 sum.
# Wait until 02:00 UTC before new S7 entries so the flip settles.
REENTRY_DELAY_HOURS = 2
# Path losses (not SL hits) dominate S7 red. Flat and wait for next flip if
# adverse move from entry reaches this % — different from the rejected hard TP.
SOFT_LOSS_PCT = 0.025  # 2.5% — 1.75% was cutting winners on $50 noise


def _btc_prior_day_up(exchange):
    daily = exchange.fetch_ohlcv(BTC_SYMBOL, '1d', limit=3)
    if not daily or len(daily) < 2:
        return None
    y = daily[-2]
    return (y[4] - y[1]) / y[1] > 0


def strategy7_signal(exchange, symbol):
    now = datetime.now(timezone.utc)
    if now.hour < REENTRY_DELAY_HOURS:
        return None, (
            f"Post-flip delay — no S7 entries before {REENTRY_DELAY_HOURS:02d}:00 UTC "
            f"(now {now.hour:02d}:{now.minute:02d})"
        )

    daily = exchange.fetch_ohlcv(symbol, '1d', limit=ATR_PERIOD + 6)
    if not daily or len(daily) < ATR_PERIOD + 2:
        return None, "Not enough daily data"

    df = pd.DataFrame(daily, columns=['ts', 'open', 'high', 'low', 'close', 'volume'])
    yesterday = df.iloc[-2]  # last completed UTC day
    y_ret = (yesterday['close'] - yesterday['open']) / yesterday['open']
    if y_ret == 0:
        return None, "Prior day flat — no direction"

    side = 'long' if y_ret > 0 else 'short'
    if side == 'short' and LONG_ONLY:
        return None, "Long-only mode — S7 short skipped"
    if side == 'short' and BLOCK_SHORTS_WHEN_BTC_UP:
        btc_up = _btc_prior_day_up(exchange)
        if btc_up is True:
            return None, "Short blocked — BTC prior day was up (Tier A)"
        if btc_up is None:
            return None, "Short blocked — BTC trend check unavailable"

    price = float(df['close'].iloc[-1])  # current (in-progress) daily close = last price

    atr_d = ta.atr(df['high'], df['low'], df['close'], length=ATR_PERIOD)
    atr = float(atr_d.iloc[-1])
    if not atr or atr <= 0:
        return None, "ATR unavailable"

    if side == 'long':
        stop_loss = price - ATR_SL_MULT * atr
        take_profit = price + ATR_TP_MULT * atr
    else:
        stop_loss = price + ATR_SL_MULT * atr
        take_profit = price - ATR_TP_MULT * atr

    signal = {
        'symbol': symbol,
        'side': side,
        'price': price,
        'stop_loss': round(stop_loss, 4),
        'take_profit': round(take_profit, 4),
        'prev_day_ret_pct': round(y_ret * 100, 2),
        'reason': f"Daily momentum: prev day {y_ret*100:+.2f}%, hold {side} until flip",
        'strategy': 'S7_Daily_Momentum',
        'order_type': 'ioc',
        'time': datetime.now(timezone.utc).isoformat(),
    }
    return signal, "✅ SIGNAL FOUND"
