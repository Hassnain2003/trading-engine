# Crypto Agent — Architecture (from code, audit 2.5)

This document describes the system as implemented. It is written from the
code, not from memory, and is kept in sync with each stage.

## 1. Phase and equity

- **Phase**: `micro_500_2026-10` — $500 starting equity, pre-live/demo.
  Defined in `config.yaml` (`phase.*`) and `trading_phase.py`.
- **Equity source**: Bybit demo wallet via `fetch_balance`. If the wallet
  cannot be set to $500 (Bybit `demo-apply-money` returns 10016), set
  `FORCE_EQUITY=1` so `phase_equity(wallet) = 500 + (wallet_now -
  wallet_at_phase_start)` tracks a $500 baseline off wallet deltas.
- **DB**: `agent_trades_micro_500_2026-10.db` (WAL mode). The archived
  34k-demo DB (`agent_trades_34k_phase.db`) is read-only and never mixed
  into live stats.
## 9. Daily loss breakers

`RiskManager.update_balance()` returns `'flatten'` (−4%), `'lock'` (−2%),
or `None`. The engine pauses entries on lock/flatten but lets open
positions run to SL/TP. Breakers are disarmed until a real equity figure
is established (prevents the 2026-08-26 phantom-flatten incident).

## 10. Ledger and attribution (Stage 1)

- `exchange_fills` (UNIQUE exec_id) and `exchange_closed_pnl`
  (UNIQUE order_id): append-only, idempotent via `INSERT OR IGNORE`.
- `signals` carries 15 new columns including `planned_risk_usd`,
  `git_hash`, `config_hash` (audit 2.1: hash of effective config).
- `config_hash()` is stable across runs (callables → qualname, memory
  addresses stripped).

## 11. What is NOT in this system

- No architecture document existed before this file (verified against full
  git history).
- No FK links signals↔orders; matching is heuristic (symbol + timestamp +
  price proximity).
- ccxt and google.genai are unusable under the current interpreter
  (Python 3.12 site-packages under Python 3.14) — live exchange queries
  and LLM jobs fail at import.
## 6. Min-lot bump (audit 2.4)

`ensure_min_exchange_qty()` bumps sub-min-lot orders to the exchange
minimum only when:

- margin ≤ 40% of balance, AND
- min-lot stop-risk ≤ 2% of equity hard cap.

On every bump it logs actual stop risk, notional, margin, and the
strategy notional cap. A WARNING fires when min-lot notional exceeds the
strategy notional cap. If stop-risk exceeds the 2% cap, the entry is
refused (returns 0).

At current prices (BTC ~$81.9k, ETH ~$2.40): S7 BTC and S9 BTC trigger
bump+notional-warning on every entry; S9 ETH triggers notional-warning
only; S7 ETH never triggers.

## 7. Correlation protections

- **`correlation_cap_active`** (audit 2.2): weekly job runs Sunday ≥06:00
  UTC. If any pair exceeds `CORRELATION_THRESHOLD` (0.85), the position
  cap drops to 2 for the week; cleared on Monday UTC midnight. Wired and
  tested, but the verdict is data-driven.
- **`check_correlation_override()`** (`risk_manager.py`): blocks a same-side
  signal on another symbol within 15 minutes. **Wired but OFF**
  (`CHECK_CORRELATION_OVERRIDE = False` in `entry_scanner.py`) pending a
  user decision on the audit counterfactual (20.7% of 34k-era signals
  would have been blocked; S7-on-ETH blocked 50%).

## 8. Kill switch (strategy pause)

`db_manager.strategy_disabled()` / `set_strategy_disabled()` persist a per-
strategy disabled flag in `kv_state` (`strategy_disabled:<module>`).
`entry_scanner.scan_entries()` skips disabled strategies. The engine
HTTP API exposes `/kill_strategy/<name>` and `/enable_strategy/<name>`.
## 3. Risk configuration (single source of truth — audit 2.1)

All sizing parameters come from `config.yaml` with `trading_phase.py`
overrides and `SECTION__KEY` environment-variable overrides applied last:

- `config.yaml [strategy_risk]` — per-strategy risk dial (S2 0.8%,
  S7 0.8% via `micro_strategy_risk`, S9 1.0%, default 1.0%).
- `config.yaml [micro_strategy_risk]` — $500-phase overrides (S2 1.5%,
  S7 0.8%).
- `config.yaml [notional_cap]` — per-strategy notional caps
  (S2 25%, S7 12%, default 10%).
- `config.yaml [risk]` — `max_trade_risk_pct` (2% hard ceiling),
  `daily_loss_lock_pct` (2%), `daily_flatten_pct` (4%),
  `weekly_kill_switch_pct` (7%), `max_positions` (16),
  `max_weekend_positions` (14).
- `config.yaml [execution]` — `leverage` (10x default), plus IOC,
  poll interval, etc.

`RiskManager.effective_config()` returns the merged, sorted dict; it is
printed at startup and fed into `config_hash()`.

## 4. Position sizing

`RiskManager.calculate_position_size(entry, stop, side, strategy)`:

1. Risk budget = equity × strategy risk dial, capped by `max_risk_usdt()`
   (equity × 2% hard cap).
2. Drawdown taper (`drawdown_size_multiplier()`) shrinks size toward 0 as
   daily P&L approaches −2% lock.
3. Min stop distance = 0.5% of entry price.
4. Raw qty = risk / max(stop_distance, min_stop).
5. Notional cap: qty reduced so notional ≤ equity × strategy notional cap.
6. Final hard-cap check: dollar risk ≤ equity × 2%.

`RiskManager.risk_pct_for_strategy()` canonicalizes label keys
(`S7_Daily_Momentum` → `strategy7_daily_momentum`) so both forms resolve.

## 5. Portfolio heat (audit 2.3)

`RiskManager.portfolio_heat_ok()` gates each candidate entry:

- **Open positions** contribute their STORED `planned_risk_usd`
  (actual qty × stop distance recorded at entry). Legacy/adopted rows
  without a stored value fall back to equity × nominal dial.
- **Candidate** contributes `new_planned_risk_usd` (post-min-lot-bump qty
  × stop distance), computed in `try_entry()` after the min-lot bump.
- **Aggregate cap**: total risk ≤ `MAX_PORTFOLIO_RISK_PCT` (6%) of equity.
- **Correlated cluster**: positions with |corr| ≥ `CORR_ENTRY_THRESHOLD`
  (0.7) share a `MAX_CORRELATED_RISK_PCT` (3%) sub-cap.

The effective position limit at $500 is heat-bound (~6–7 positions from
real stop risk of $4–7.50 each), not the nominal 16.
## 2. Strategies (single source of truth: `execution/config.py`)

`STRATEGY_CONFIG` lists (module, symbols); `ACTIVE_STRATEGIES` gates what
the engine loads:

| Strategy | Module | Symbols | Status |
|---|---|---|---|
| S2 Breakout Retest | `strategy2_breakout_retest` | BTC | Active |
| S7 Daily Momentum | `strategy7_daily_momentum` | BTC, ETH | Active |
| S1 RSI Reversion | `strategy1_rsi_reversion` | BTC, XRP | Not active |
| S3 VWAP Bands | `strategy3_vwap_bands` | XRP, ETH | Disabled 2026-07-08 |
| S4 RSI MACD | `strategy4_rsi_macd` | BTC, ETH, LINK, DOGE | Disabled 2026-07-08 |
| S5 MA Crossover | `strategy5_ma_crossover` | DOGE, BNB, SUI | Not active |