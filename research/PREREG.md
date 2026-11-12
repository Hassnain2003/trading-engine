# Research Preregistration — Crypto Agent Strategy Evaluation

**Purpose:** These eligibility rules are fixed BEFORE any results from sections 3.3–3.5 are computed. They cannot be edited after seeing the out-of-sample results. This prevents p-hacking and multiple-testing bias.

**Status:** PROPOSED — pending user approval. Do not run 3.3–3.5 until approved.

---

## Eligibility Rules (Out-of-Sample Data Only)

A strategy/symbol/side combination is **eligible for live capital** only if ALL of the following hold on the out-of-sample (walk-forward test) data:

| # | Rule | Threshold |
|---|------|-----------|
| E1 | Net expectancy per trade | ≥ +0.15 R |
| E2 | Bootstrap 90% lower bound on expectancy | > 0 |
| E3 | Net profit factor | ≥ 1.30 |
| E4 | Random-entry p-value | < 0.05 |
| E5 | Positive in rolling OOS windows | ≥ 3 of 4 windows |
| E6 | Maximum drawdown | ≤ 15% |
| E7 | Robust to cost stress | Still positive with fees × 1.5 AND slippage × 2 |

All seven rules must pass simultaneously. Failing any one rule makes the combination ineligible.

---

## Metric Definitions

- **Expectancy in R**: average P&L per trade divided by the initial stop risk (R) for that trade. Net of fees and slippage.
- **Net PF**: gross profit / gross loss, net of all costs.
- **Bootstrap 90% CI**: 10,000 resamples with replacement of the trade P&L distribution; 5th and 95th percentiles.
- **Random-entry p-value**: fraction of 1,000 random-entry runs (same exits, stops, sizing, side mix, trade count) with net P&L ≥ strategy net P&L.
- **Rolling OOS windows**: 4 consecutive 3-month test windows from the walk-forward (section 3.5). A window is "positive" if net P&L > 0.
- **Max drawdown**: largest peak-to-trough decline in cumulative equity, computed on the OOS equity curve.
- **Cost stress**: fees = 5.5 bps × 1.5 = 8.25 bps per side; slippage = 3 bps × 2 = 6 bps per side.

---

## Data

- Source: Bybit public REST (mainnet), no API key.
- Symbols: BTC/USDT:USDT, ETH/USDT:USDT.
- Timeframes: 5m, 15m, 1d.
- Lookback: ≥ 3 years.
- Fixed date cutoff: to be recorded after download completes (see `data/checksums.sha256` and `data/gap_report.txt`).

---

## Multiple-Testing Correction

If N variants are tested (see `research/EXPERIMENTS.md`), the family-wise error rate is not controlled by the per-rule p-value threshold alone. With N independent tests at α = 0.05, expected false positives ≈ 0.05 × N. The random-entry p-value (E4) is computed per variant; readers should apply Bonferroni (α/N) or interpret results with N in mind.

---

## Approval

Rules approved by user: ☐ Yes ☐ No — edits requested

If edits requested, return a revised PREREG.md before 3.3–3.5 are executed.