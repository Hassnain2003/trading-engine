# Stage 3 — Production Semantics Trace

This document records, **before any Stage 3 result is computed**, what the live
engine actually does — traced from source, not from memory. Every line cites
the file it came from. The walk-forward harness (`research/`) reproduces these
behaviours; where the harness must pick a deterministic convention that the
live code leaves to the exchange (intrabar ordering, liquidation), the choice
is recorded in §8 with its rationale.

Notation: `entry` = the price the signal names; `fill` = the price the trade
actually transacts at.

---

## 1. Entry (signal → order)

| Step | Behaviour | Source |
|------|-----------|--------|
| 1 | Engine runs `scan_entries` **once per new closed 5m bar** (and once per new closed 15m bar). The reference bar is BTC 5m; a scan is skipped if that bar is older than `STALE_BAR_MAX_AGE_SEC` (600 s). | `execution/engine.py:732-772`, `utils/bars.py` |
| 2 | Per strategy, a **daily entry cap** of `MAX_ENTRIES_PER_STRATEGY_PER_DAY` (3) applies, counted per UTC day. | `execution/config.py:71`, `execution/entry_scanner.py:309-343` |
| 3 | Per strategy, symbols are iterated in `STRATEGY_CONFIG` order. | `execution/config.py:18-32` |
| 4 | **Liquidity gate**: spread ≤ 0.05 % and last completed 1h notional volume ≥ $2 M (majors). | `utils/gate.py` |
| 5 | The strategy function is called with the *public* exchange and returns `(signal, msg)` or `(None, msg)`. | `execution/entry_scanner.py:141` |
| 6 | `risk_manager.can_open_trade()` gate: blocks on un-established equity, cooldown, daily-loss lock, daily-profit-target hit, weekly kill switch, position-count cap. | `risk/risk_manager.py:247-262` |
| 7 | **One live owner per contract**: if the symbol already has an open/pending signal *or* a non-zero exchange position, the entry is skipped. This prevents strategies from stacking onto each other on shared BTC. | `execution/entry_scanner.py:30-40`, `175-192` |
| 8 | Order type: S2/S7 set `order_type='ioc'`, so the branch `is_ioc` is taken → `place_s3_marketable_order` → a **IOC limit** priced by walking the book, capped at ±0.2 % from best. | `execution/entry_scanner.py:242-250`, `execution/order_manager.py:30-67` |

**Consequence for the harness:** every entry is an IOC *taker* fill. There is no
maker leg, so fee classification is unambiguous (§6).

**Consequence for the harness:** because a symbol can host only one position,
the harness must serialise strategies per symbol rather than running them in
parallel.

### 1.1 Signal-specific entry rules

**S2 — Volume-Confirmed Breakout Retest** (`strategies/strategy2_breakout_retest.py`)

1. Blocked UTC hours: `{0, 1, 2, 11}`.
2. Session = UTC day. Opening range = the first 15 minutes (3 × 5m bars) of the
   session, taken from `fetch_ohlcv(symbol, '5m', since=day_start, limit=300)`.
3. Trend filter: 1h EMA ribbon (9/21/50) over the **last 50 1h bars**;
   bullish = fast > medium > slow, bearish = the inverse.
4. Volume filter: breakout candle volume ≥ 1.5 × mean volume of the last 25
   5m bars **excluding the current (forming) candle**.
5. Retest: current price within ±1 % of the session extreme; the two candles
   before the current one must both have closed on the far side of the level.
6. `LONG_ONLY = True` → the bearish branch is dead code.
7. Stop: `session_high × 0.995` (long), then widened by `_apply_min_stop` so the
   distance is ≥ `MIN_STOP_PCT` (1.2 %); TP rebuilt in R at
   `TP_R_MULTIPLES['BTC/USDT:USDT'] = 3.0`.
8. Entry price = `current_price` = the close of the **forming** 5m bar.

**S7 — Daily Momentum Continuation** (`strategies/strategy7_daily_momentum.py`)

1. No entries before `REENTRY_DELAY_HOURS` = 02:00 UTC (post-flip settle).
2. Direction = sign of the **last completed UTC day**'s `(close - open) / open`.
3. `LONG_ONLY = True` → shorts are skipped outright (the BTC-prior-day filter
   is therefore unreachable).
4. ATR(14) on the daily series (`limit = ATR_PERIOD + 6` = 20 bars) sizes the
   guard: SL = 2 × ATR, TP = 4 × ATR.
5. Entry price = the close of the **forming** daily bar (i.e. the live price).

---
## 2. Position sizing

`risk_manager.calculate_position_size(entry, stop, side, strategy)` —
`risk/risk_manager.py:361-399`

1. `risk_pct` = effective per-strategy dial. The precedence is
   `config.yaml [strategy_risk]` → `config.yaml [micro_strategy_risk]` →
   `trading_phase.MICRO_STRATEGY_RISK_PCT` → `SECTION__KEY` env vars
   (`risk/risk_manager.py:22-30, 50-62`). The harness calls
   `effective_strategy_risk()` directly so it can never drift from production.
   The result is capped by `MAX_TRADE_RISK_PCT` (2 %).
2. `risk_per_trade = min(equity × risk_pct, equity × MAX_TRADE_RISK_PCT)`.
3. **Drawdown taper**: `drawdown_size_multiplier()` scales risk linearly to
   zero as the daily P&L approaches the −2 % lock.
4. `risk_per_unit = max(|entry − stop|, entry × 0.005)` — the 0.5 % floor on
   stop distance. The 0.005 is **hard-coded** in `risk_manager.py:375`;
   `config.yaml risk.min_stop_pct` is *not* read by this function (it is a
   dead config key — a known drift, noted in §9).
5. `raw_qty = risk_per_trade / risk_per_unit`.
6. **Notional cap**: `equity × notional_cap[strategy]` (S2 25 %, S7 12 %,
   default 10 %) caps `raw_qty × entry`.
7. Re-check against the 2 % per-trade hard cap; return 0 if it still exceeds.

Then in `entry_scanner.try_entry` (`execution/entry_scanner.py:199-241`):

8. Regime multiplier: `apply_regime_size_multiplier(adx)` → 0.5 if ADX > 40
   else 1.0. S2/S7 do not emit `adx`, so the default 20 applies → **1.0**.
9. Correlation size multiplier: needs a correlation matrix the harness does
   not have → **1.0** in replay (documented deviation, §8).
10. `ensure_min_exchange_qty` may bump the size up to Bybit's minimum lot, but
    only if the min-lot stop-risk fits under
    `MIN_LOT_MAX_STOP_RISK_PCT` (2 %) of equity and the margin fits under 40 %
    of equity; otherwise the entry is **skipped entirely**.
11. **Portfolio heat**: the candidate's *actual* stop risk (`qty × |entry − stop|`)
    plus the stored planned risk of all open positions must stay under 6 % of
    equity; the correlated-cluster cap (3 %) needs a matrix → skipped in replay.

**Consequence for the harness:** sizing is path-dependent (equity at entry time)
but fully deterministic given the equity curve. Replay reproduces it exactly
because equity is the replay book, not a venue read.

**Replayed risk dials** (verified by calling `effective_strategy_risk()` in
this environment — it returns the *micro-phase* values, because
`trading_phase.MICRO_STRATEGY_RISK_PCT` wins last):

```
{'strategy2_breakout_retest': 0.015, 'strategy7_daily_momentum': 0.008,
 'default': 0.01}
```

Notional caps (`effective_notional_caps()`):
`{'strategy7_daily_momentum': 0.12, 'strategy2_breakout_retest': 0.25,
'default': 0.10}`.

---
## 3. Exits

The live engine has **several** distinct exit paths. All of them close the
whole position on that symbol (one-way mode, one owner per symbol).

| # | Exit | Trigger | Source |
|---|------|---------|--------|
| E1 | **Stop loss** | Bybit's attached `tpslMode: Full` SL, `slTriggerBy: LastPrice`, `slOrderType: Market`. | `execution/order_manager.py:134-175` |
| E2 | **Take profit** | The attached TP, `tpTriggerBy: LastPrice`. | same |
| E3 | **Trailing stop** | Maintenance cycle: for S2/S7, the SL is moved to `mark × (1 − trail)` (long) / `mark × (1 + trail)` (short), never widening. Trails: S2 1.5 %, S7 2.0 %. | `execution/position_manager.py:142-146`, `179-245` |
| E4 | **Signal reversal** | Every cycle, the strategy function is re-run for each open position; if the new signal's side opposes the position, the position is closed (`exit_reason='signal_reversal'`). | `execution/entry_scanner.py:349-410` |
| E5 | **S7-specific** | (a) soft loss cap: adverse move from entry ≥ `SOFT_LOSS_PCT` (2.5 %) → close; (b) day-flip: on a UTC day boundary where the prior day's direction opposes the position → close. Long-only, so a "flip to short" closes the long and stays flat. | `execution/s7_manager.py:87-192` |
| E6 | **Risk breaker** | Daily P&L ≤ −4 % → flatten everything; ≤ −2 % → block new entries; +2 % daily target → block new entries; 7 % rolling drawdown → halt entries. | `risk/risk_manager.py:170-196`, `204-245` |
| E8 | **Max position age** | `MAX_POSITION_AGE_SEC` (7 days) → close. | `execution/engine.py:487-495`, `execution/config.py:70` |

**Consequence for the harness:** E1/E2 are exchange-hosted and trigger on
`LastPrice` — i.e. on the **last traded price**, not on the bar close. The
harness resolves them against the 5m bar's high/low (§4).

**Consequence for the harness:** E3 (trailing stop) is a *maintenance-cycle*
action driven by `markPrice`, so it must be evaluated on the same cadence as
the live engine's poll interval (300 s) — practically, once per 5m bar in
replay, which is the granularity at which the exchange could have moved the
stop anyway.

---



## 4. Costs and funding

### 4.1 Fees

- Every fill is a **taker** fill (§1 step 9), so the taker rate applies on
  both sides. `BYBIT_TAKER_PCT = 0.055` is a *percentage of notional*
  (0.055 %) = **5.5 bps per side**, 11 bps round trip.
  Source: `utils/stats.py:11`.
- The harness charges `fee = |qty| × fill_price × 0.00055` on entry and again
  on exit. Funding is **not** folded into the fee.

### 4.2 Slippage — the one open conflict

| Source | Slippage assumption |
|--------|---------------------|
| `research/PREREG.md:35` (preregistered) | **3 bps per side** (6 bps round trip) |
| `config.yaml:60-61` → `utils/stats.py:16-19` | **0.5 bps per side** (1 bps round trip) |

The preregistration is the *contract with the reader*; the config value is the
production *accounting* dial used by `utils/stats.py` for gate checks. They
disagree by 6×.

**Resolution adopted (must be confirmed before results are published):**

1. The **baseline** Stage 3 run uses the **preregistered 3 bps/side**. This is
   the honest, conservative number and is what PREREG E7 cost-stress
   multiplies.
2. The production 0.5 bps/side is reported as a **secondary "production-cost"
   run** for reference only, clearly labelled as *not* the preregistered
   configuration.
3. The preregistered stress test (E7) is applied to the 3 bps baseline:
   fees 8.25 bps/side, slippage 6 bps/side.

Slippage is applied as a **fill-price penalty**, not a cash charge:
- Long entry fills at `price × (1 + s)`, long exit at `price × (1 − s)`.
- Short entry fills at `price × (1 − s)`, short exit at `price × (1 + s)`.
- `s` = slippage in decimal (3 bps = 0.0003).

This makes the cost a genuine P&L reduction rather than a bookkeeping
subtraction, and it is symmetric.

### 4.3 Funding

- **Cadence**: exactly every 8 h at 00:00 / 08:00 / 16:00 UTC, verified over
  the full sample — 3284 gaps, every one exactly 8.000 h, timestamps always
  on the hour with minute = 0. (`research/funding_stats.py`)
- **Magnitudes** (BTC / ETH, 3285 rows each, 2023-10-21 → 2026-10-19):
  median +0.0061 % / +0.0066 %, mean +0.0069 % / +0.0069 %,
  1st pct −0.0091 % / −0.0095 %, min −0.0294 % / −0.1177 %,
  negative in 17.4 % / 18.9 % of intervals, |rate| > 0.01 % in ~9–10 %.
- **Payment**: a long pays `notional × rate` when the rate is positive; a
  short receives it. The harness applies funding **at the 8 h timestamp**, to
  any position open at that instant, using the position's notional at the
  funding mark.
- **Order within a bar**: funding is applied **before** the bar's exit checks,
  because the funding timestamp (00:00/08:00/16:00) is a bar boundary and
  Bybit settles on the mark price at that instant. Positions opened later in
  the same bar do not pay for the interval that already elapsed.
- **Sign convention verified against production**: `position_sync.py:84`
  computes `net = gross − entry_fee − exit_fee + funding_val`, i.e. the
  `funding_payments.amount` column is stored **signed from the account's
  perspective** (negative = paid). The harness uses the same convention.

**Consequence for the harness:** funding is a real, non-trivial carry term
(≈ 0.02 %/day average on notional, with tails). Ignoring it would bias
multi-day holds. It must be in the ledger as a separate column so the
"gross of funding" and "net of funding" numbers can both be reported.

---
## 5. Time and the "forming bar" problem

This is the single most important determinism issue in the whole harness.

### 5.1 Wall-clock reads inside the strategies

| Strategy | Clock read | Effect |
|----------|------------|--------|
| S2 | `datetime.now(timezone.utc)` at `strategy2_breakout_retest.py:94` | picks the UTC **session day**, the blocked-hour check, and the opening-range window. The day boundary at 00:00 UTC changes the opening range and the session extreme. |
| S2 | `datetime.now(timezone.utc)` at `:161`, `:190` | only the reported signal timestamp. |
| S7 | `datetime.now(timezone.utc)` at `strategy7_daily_momentum.py:60` | the `REENTRY_DELAY_HOURS` (02:00 UTC) gate and the daily-bar identity. |
| S7 | `datetime.now(timezone.utc)` at `:111` | reported timestamp only. |

**Rule for the harness:** the module-level clock must be pinned to the
*simulated* time, and the pinned time must be the time of the **bar being
evaluated** (i.e. the bar's close timestamp), because that is what the live
engine would have seen when the bar closed and the scan fired.

Concretely: when replaying the 5m bar that closed at `T`, the pinned clock is
`T` (the bar's close time), and the strategy is allowed to see the bar that
ends at `T` plus the **forming** bar that starts at `T` (which, in the live
engine, is the next bar in progress).

### 5.2 The forming bar

Every strategy in this system uses `fetch_ohlcv(...)` and treats
`bars[-1]` as "the current bar" — which on a live exchange is the **still
forming** bar whose close equals the live last price.

`ReplayExchange` reproduces Bybit's exact behaviour here: `fetch_ohlcv`
returns the candle at `since` plus the currently forming final candle. The
harness therefore calls the strategy **as the live engine would, at the same
instant in the bar lifecycle**, and takes the strategy's own view of
`bars[-1]` as authoritative.

**Rule:** the harness evaluates each strategy at the **close of each 5m bar**
and reconstructs the "forming bar" view by asking `ReplayExchange` for the
series with `since` set so that the final element is the bar in progress at
the evaluation instant. In practice this means: when evaluating the bar that
closed at `T`, the exchange serves the bar `[T, T+300s)` as forming, whose
open = close of previous bar and whose current price = `T`'s close.

Because the strategies fetch their own data through the exchange interface,
they get the forming bar automatically, exactly as in production. The
harness only needs to (a) pin the clock, and (b) guarantee the exchange
returns the right *partial* bar.

### 5.3 Signal evaluation cadence

`STRATEGY_TIMEFRAME` (`execution/config.py:41-50`) registers both replayed
strategies on `'5m'`:

```python
'strategy2_breakout_retest': '5m',
'strategy7_daily_momentum':  '5m',
```

So the live engine calls `scan_entries(..., timeframes={'5m'})` on every new
closed 5m bar, and both strategies are evaluated on every 5m bar, regardless
of the timeframe they internally read (S2 reads 5m + 1h, S7 reads 1d). The
comment at `config.py:45-47` states this explicitly for S7: "scanning each 5m
close is just the retry cadence".

**Rule:** the harness evaluates S2 and S7 on **every closed 5m bar**, in
`STRATEGY_CONFIG` order, and applies the per-strategy daily entry cap of 3
across those evaluations.

Position management (E3 trailing, E4 reversal, E5 S7) also runs every
engine cycle. `POLL_INTERVAL_SEC` = 300 s, so once per 5m bar in replay.
Order within a cycle, from `execution/engine.py`:
1. `_run_maintenance()` — reconcile, trailing stops, S7 flips, S9 flips.
2. `check_signal_reversal_exits()` (E4).
3. `scan_entries()` for the new 5m bar, then for the new 15m bar.

**Rule:** the harness reproduces this order per 5m bar: **management first,
then reversal check, then entry scan.** This ordering matters — a trailing
stop that fires in the maintenance phase must be reflected before the
reversal check and before a new entry is considered on that symbol.

---

## 6. Deterministic resolution rules

The live code delegates several things to the exchange, which the harness
cannot. Every such case is resolved by an explicit, stated rule. These rules
are **fixed before results are computed**.

### 6.1 Intrabar stop / target ordering (R1)

The exchange watches `LastPrice` and fires the moment the last trade crosses
the trigger. On 5m bars we only know `open, high, low, close`.

**Rule R1 — pessimistic-first:** within a bar, if **both** the stop and the
target are inside the bar's range, the **stop is assumed to fill first**.
Rationale: the price path within a bar is unknown, and the stop-first
assumption is the conservative one. It never inflates returns.

If only one trigger is inside the range, that one fills. If neither, the
position survives the bar.

**Fill prices:**
- Stop: filled at the stop price exactly (a market stop at the trigger
  price, before slippage).
- Target: filled at the target price exactly.
- Both then get the slippage penalty of §4.2 applied on the exit side.

**Consequence:** a bar whose range spans more than the stop distance AND the
target distance is always a loss. This is the single largest source of
replay-vs-live divergence, and it is deliberately biased against the
strategy.

### 6.2 Which bar's high/low are used (R2)

**Rule R2 — bar-close evaluation with intrabar path:** exits are checked
against the 5m bar that just closed, using that bar's `high`/`low`.

For the double-trigger case R1 already picks the stop. For the
single-trigger case the assumption is always consistent: if only the stop is
triggered then `low <= stop`, and since `close ∈ [low, high]` the close
cannot already be beyond the stop in a way that contradicts the fill.

**Consequence:** the harness does not need to guess a path beyond the
stop-vs-target ordering.

### 6.3 Gap handling (R3)

A bar whose `open` is already beyond the stop (gap through the stop) fills
at the **bar open**, not at the stop price — a market stop fills at the next
available price, which is the open of the gap bar. The same applies to a
target: if the bar opens beyond the target, the TP fills at the **open**
(limit orders fill at better prices).

**Rule R3:** compare `open` against the trigger first; if `open` has already
crossed the trigger, fill at `open`; otherwise use the bar range.

### 6.4 Trailing stop (R4)

**Rule R4 — evaluated on bar close, applied to the *next* bar:** the live
engine trails on `markPrice` during the maintenance phase, which happens
*after* the previous bar closed. The harness therefore:

1. At the close of bar `i`, compute the new trailing stop from bar `i`'s
   close (used as the mark) and update the stop for bar `i+1` onward.
2. The stop set at step 1 is checked against bar `i+1`'s range.

The trail never widens the stop: the new stop is `max(old_stop, trail_stop)`
for a long and `min(old_stop, trail_stop)` for a short.

**Consequence:** the trailing stop is never active *within* the bar in which
it is computed. The live engine can trail several times inside a 5m bar
(poll interval = bar length, so in practice once).

### 6.5 Signal reversal exit (R5)

**Rule R5 — fill at the bar close:** E4 fires when the strategy function
returns an opposing signal. The live engine closes via `close_positions`,
i.e. a market order, at whatever the last price is. In the harness the
strategy is evaluated with the forming bar whose close equals the last
closed bar's close, so the reversal fill is at that bar's **close**, plus
the exit-side slippage.

**Reversal is not a re-entry.** The live engine could, in principle, take a
new position in the opposite direction on the same symbol in the same cycle
(the position is removed from `risk_manager` before the next scan).

**Rule R5b:** the harness does **not** allow same-bar re-entry after a
reversal. Rationale: taking a position in the *opposite* direction on the
same bar as a reversal close produces meaningless churn, and the reversal
close and the entry scan do not see the same data. Documented deviation (§8).

### 6.6 S7 flip exit (R6)

**Rule R6 — evaluated at the 00:00 UTC day boundary:** `manage_s7_flips`
runs in the maintenance phase, reads the *last completed* daily bar
(`daily[-2]`), and closes when the prior day's direction opposes the
position. It also requires `entry_date < today` — the position must be held
at least until the next UTC day boundary.

The harness evaluates the flip check at each 00:00 UTC boundary, using the
daily bar that closed at that boundary. The fill is at the 00:00 boundary
price (the open of the first 5m bar of the new day) plus exit slippage.

The S7 soft-loss cap (E5a) is a mark-price check on every cycle:
`pnl_pct <= -SOFT_LOSS_PCT` (−2.5 %) → close at the current mark.
**Rule R6b:** the harness applies the soft loss on the 5m bar close, and
fills at that close (plus exit slippage).

### 6.8 Funding application (R8)

**Rule R8:** at each funding timestamp (00:00, 08:00, 16:00 UTC), funding is
applied to positions open **at that instant** (i.e. opened at or before the
timestamp) using the notional at the funding mark, **before** that bar's exit
checks. A position opened later in the same 5m bar does not pay.

### 6.9 Daily risk locks (R9)

**Rule R9 — evaluated on the 5m bar close against mark-to-market equity:**

- Equity = `cash_equity + Σ unrealized P&L` of open positions, marked at the
  bar close.
- `daily_pnl = equity − session_start_equity`, where
  `session_start_equity` is the equity at the previous 00:00 UTC boundary.
- `daily_pnl <= −2 %` → `daily_loss_locked = True` → **no new entries** for
  the rest of the UTC day.
- `daily_pnl <= −4 %` → **flatten all positions** at the bar close, then
  lock entries.
- `daily_pnl >= +2 %` → `daily_target_hit = True` → **no new entries** for
  the rest of the UTC day.
- Weekly kill switch: a rolling 7-day equity high-water mark, reset each
  Monday; if equity falls ≥ 7 % below the mark, entries are halted until the
  next Monday.

**Consequence:** the live code uses `current_balance` (wallet) plus
`unrealized` from the venue, and compares against a `session_start_equity`
snapshotted at the day reset. The harness reproduces this with its own book.
The one difference: the live engine's day reset happens on the first
`update_balance` call after midnight, which uses a *wallet* balance; the
harness resets at exactly 00:00 UTC using mark-to-market equity.
Documented deviation (§8).

### 6.10 Liquidation

**Rule R10 — not modelled.** A real liquidation would occur if the
position's margin were exhausted. With 10× leverage
(`DEFAULT_LEVERAGE = 10`) and positions sized to ≤ 25 % of equity notional
with a stop at 1.2 %–2.5 % distance, the stop is hit long before the
liquidation price. The harness asserts this invariant instead of simulating
liquidation: for every trade, the distance to the liquidation price is
greater than the distance to the stop.

### 6.11 One owner per symbol (R11)

**Rule R11 — serialise by symbol.** A symbol can host at most one open
position across all strategies. BTC is shared by S2, S7 and S9; ETH by S7
only. The harness enforces this exactly as `_symbol_busy` does, and records
which strategy "won" the symbol.

**Consequence:** the strategies are not independent samples on BTC. The
per-strategy metrics must be reported as *attributional* (the strategy that
owned the position), with the caveat that one strategy's presence can
suppress another's entry.


---

## 7. Walk-forward design

### 7.1 Data span and the four OOS windows

The dataset covers **2023-10-20 → 2026-10-19** (3 years, 1095 daily bars,
315 360 5m bars per symbol, zero gaps — `data/gap_report.txt`).

PREREG E5 requires "4 consecutive 3-month test windows". The harness
therefore reserves the **final 12 months** as out-of-sample test data, split
into four consecutive 3-month windows:

| Window | Test period (UTC) | Days |
|--------|-------------------|------|
| W1 | 2025-10-21 → 2026-01-21 | 92 |
| W2 | 2026-01-21 → 2026-04-21 | 90 |
| W3 | 2026-04-21 → 2026-07-21 | 91 |
| W4 | 2026-07-21 → 2026-10-19 | 90 |

Each window is preceded by a **trailing** training window of 12 months
(anchored at the window's start, i.e. the most recent 12 months before it).
Because this harness contains **no fitted parameters** — the strategies are
the live code, unmodified — the training window is used only to:
1. warm up the indicators (the 432-period SMA needs ~3 days of 5m bars;
   the 1h EMA ribbon and the 1d ATR need their own history), and
2. establish the equity baseline for the risk manager (equity compounds
   across the training period).

**The walk-forward is therefore a *segmented replay*, not a parameter sweep.**
The harness replays the entire 3 years continuously and then *reports* the
four OOS windows. Positions opened in the training window and still open at
the start of a test window are carried into it, because that is what a live
deployment would do — but they are flagged `carried_in` and reported
separately.

**Rationale for reporting rather than re-fitting:** PREREG §"Multiple-Testing
Correction" says N variants are tested and the reader should apply
Bonferroni. Re-fitting per fold would add an un-preregistered degree of
freedom. The preregistered rules are evaluation rules, not fitting rules.

### 7.2 Fold boundaries and the "no lookahead" property

The harness advances through the dataset **strictly forward in time**. At
simulated time `T`:

- `ReplayExchange` serves only bars with `ts + interval <= T` (complete) plus
  the one forming bar.
- The strategies' own indicator windows therefore contain no future data.
- The harness's exit/management logic only sees bar `i`'s OHLC after bar `i`
  has closed.

**Parity test:** a "future-blind" variant of the harness, in which every
bar after `T` is replaced by a constant, must produce a **bit-identical**
trade list up to `T`. This is the strongest available check against
lookahead and is implemented as `tests/test_replay_no_lookahead.py`.

### 7.3 Sizing/equity across folds

Because sizing is a function of current equity, the replay carries equity
continuously across the whole 3 years. The reported OOS metrics for each
window use the equity curve segment of that window. The starting equity is
`config.yaml phase.starting_equity` = **500.0 USDT** (the micro phase),
matching the live account.

---

## 8. Documented deviations from production

Every item here is a place where the harness cannot be identical to the live
engine. They are listed so a reader can price the difference.

| # | Deviation | Direction of bias | Note |
|---|-----------|--------------------|------|
| D1 | No order-book depth. Entries fill at the signal price ± slippage; the live IOC walks the book up to 0.2 % (`IOC_MAX_CROSS_PCT`). | Optimistic on entries (real IOC may fill worse) | Partly offset by the 3 bps/side slippage assumption. |
| D2 | No partial fills / no IOC rejection. A live IOC can be rejected entirely if the book is thin; the harness always fills. | Optimistic (a rejected entry changes the trade sequence) | The liquidity gate (D5) is the proxy. |
| D3 | Correlation size multiplier and correlation-cluster cap are skipped (no correlation matrix offline). | Neutral-to-optimistic (positions can be larger than production) | `correlation_cap_active` is False in production too unless a matrix is loaded. |
| D4 | Same-bar re-entry after a signal reversal is not allowed (R5b). | Neutral | Prevents churn artefacts. |
| D5 | The liquidity gate (spread ≤ 0.05 %, 1h notional ≥ $2 M) needs a live order book / ticker. In replay, spread is assumed 0 and the 1h notional is computed from the downloaded volume. | Optimistic (the real spread is never 0) | Effectively neutral: the 1h-volume test is the binding one and is reproduced faithfully. |
| D6 | The daily-risk reset uses mark-to-market equity at exactly 00:00 UTC; production resets on the first wallet balance after midnight. | Neutral | The difference is at most one bar of unrealised P&L. |
| D7 | The trailing stop is evaluated once per 5m bar, on the bar close, and is active from the next bar (R4). | Slightly optimistic (production can trail once per poll, and the poll can land mid-bar) | Under 5m granularity this is immaterial. |
| D8 | Signal reversal / S7 soft-stop fills are at the bar close (R5, R6b). | Neutral | These are market closes; the bar close is the best available estimate of the last price. |
| D9 | Liquidation is not simulated (R10); the harness asserts the stop is always reached first. | Neutral | Verified as an invariant, not assumed. |
| D10 | News shadow / kill-switch / alert side effects are not replayed. | Neutral | No P&L impact; `strategy_disabled` is read as always False. |
| D11 | Funding is applied at the 8 h mark using the 5m bar close as the mark (R8). | Neutral | Bybit settles on the index/mark; the 5m close is within 0.05 % of it. |

---

## 9. Open items requiring a decision before publication

1. **Slippage baseline** — 3 bps (preregistered) vs 0.5 bps (production).
   §4.2 proposes 3 bps baseline + 0.5 bps as a labelled secondary run.
2. ~~The `micro_strategy_risk` vs `strategy_risk` precedence~~ — **RESOLVED.**
   `effective_strategy_risk()` applies, in order: `config.yaml
   [strategy_risk]`, `config.yaml [micro_strategy_risk]`,
   `trading_phase.MICRO_STRATEGY_RISK_PCT`, then env. The phase dict wins,
   so the replayed dials are S2 1.5 %, S7 0.8 %, default 1.0 %
   (§2). The harness must call `effective_strategy_risk()` rather than read
   the YAML, so it cannot drift.
3. **Min-lot handling** — `ensure_min_exchange_qty` may *increase* size to
   Bybit's minimum. The harness needs the Bybit lot-size table per symbol
   (fetched once and frozen into the repo) to reproduce this exactly.
   `ReplayExchange` must therefore expose a `market(symbol)` method
   returning `{'limits': {'amount': {'min': <bybit_min>}}}` and an
   `amount_to_precision(symbol, qty)`.
4. **The `risk.min_stop_pct` dead config key** — `config.yaml` sets
   `risk.min_stop_pct: 0.005`, which coincidentally equals the value
   hard-coded at `risk/risk_manager.py:375`. The harness must use the
   hard-coded 0.005 (the behaviour that is actually live), and the config
   key should be either wired up or deleted so the two cannot drift.
