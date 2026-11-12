# trading-engine

**One-line pitch:** A conservative, demo-only crypto trading engine whose live
execution path and replay/walk-forward harness share one execution semantics
specification — built to be safe by default, never to place an order by
accident.

[![CI](https://github.com/HassnainAbbas/trading-engine/actions/workflows/ci.yml/badge.svg)](https://github.com/HassnainAbbas/trading-engine/actions/workflows/ci.yml)

> **Demo/username note:** Replace `HassnainAbbas` in the badge URL above with
> your GitHub username once the repository exists.

---

## ⚠️ Disclaimer (read first)

trading-engine is an **educational and engineering project**, not financial
advice. It makes **no profit, return, or win-rate claims**, and it is **not**
tested or traded on live money unless *you* explicitly opt into mainnet (which
the engine refuses by default). The default venue is **Bybit demo/sandbox**.
Use at your own risk. You can lose real money if you override the safeties and
connect to mainnet.

---

## Architecture

```mermaid
flowchart TD
    subgraph Venue["Bybit (demo/sandbox by default)"]
      PX[Public REST / WebSocket]
      EX[Private API (demo)]
    end

    subgraph Engine["Live engine (execution/)"]
      SCAN[Entry scan · scan_entries]
      RISK[Risk manager · risk/risk_manager.py]
      ORD[Order manager · execution/order_manager.py]
      JOURNAL[Trade journal · SQLite agent_trades.db]
    end

    subgraph Replay["Replay / walk-forward harness (research/)"]
      REPLAY_EX[ReplayExchange — same semantics as live]
      REPLAY_BR[ReplayBroker — R1–R11 rules, SEMANTICS.md]
    end

    subgraph Alerting["Alerting (claude/)"]
      NTFY[ntfy push]
      DISCORD[Discord webhook]
      SMTP[Email]
    end

    PX --> SCAN
    EX --> ORD
    SCAN --> RISK
    RISK --> ORD
    ORD --> JOURNAL
    REPLAY_EX -. shared execution semantics .- SCAN
    REPLAY_BR -. same risk rules .- RISK
    JOURNAL -. KPIs / alerts .- NTFY
    JOURNAL -. KPIs / alerts .- DISCORD
    JOURNAL -. KPIs / alerts .- SMTP
```

The live path and the replay harness share execution semantics: order of
operations per bar (funding → exchange SL/TP → management → signal-reversal →
entry), IOC taker fills, one-owner-per-symbol, trailing stops, and the risk
breakers are all traced in `research/SEMANTICS.md`. The replay harness
(`research/`) reproduces them on local candle data so a walk-forward result is
directly comparable to live behaviour.

- `research/SEMANTICS.md` — production semantics trace (R1–R11), sourced from
  the live code, with the deliberate harness deviations (§8).
- `research/PREREG.md` — preregistered go-live eligibility rules E1–E7 (net
  expectancy ≥ +0.15 R, net profit factor ≥ 1.30, max drawdown ≤ 15%, etc.)
  that must pass on out-of-sample walk-forward data *before* any strategy is
  considered for live capital.

---

## Risk-manager summary (`risk/risk_manager.py`)

The risk manager is the single gate that decides whether any new entry may be
placed. It is fail-closed: it blocks trading until equity is established.

**Breakers**
- Daily loss lock: session P&L ≤ −2 % blocks new entries for the rest of the
  UTC day; ≤ −4 % flattens all positions.
- Daily profit target: hitting +2 % blocks new entries (a lock, not a quota to
  chase).
- Weekly kill switch: 7 % rolling drawdown halts entries.
- Max position age: 7 days forces a close.

**Sizing**
- Per-strategy risk dial (fraction of equity), conservative defaults: S2
  1.5 %, S7 0.8 %.
- Position size = equity × risk dial ÷ stop distance, capped by the exchange
  minimum-lot stop-risk check (a min lot whose stop risk exceeds 2 % of equity
  is skipped entirely).
- Regime and correlation size multipliers scale size down, never up beyond the
  dial.

**Heat / notional caps**
- Portfolio heat: actual stop risk of the candidate plus stored planned risk of
  open positions must stay under 6 % of equity.
- Per-strategy notional caps: S2 25 %, S7 12 %, default 10 %.

**Go-live gate** (`utils/stats.py`) — net-of-cost, live-config-scoped demo
metrics must pass profit factor > 1.3, Sharpe > 1.0, max drawdown < 15 %, and
≥ 60 trades before go-live is considered.

---

## Quick start — Bybit demo only

The engine connects to **Bybit demo/sandbox** by default and refuses mainnet
unless you explicitly set both `ALLOW_MAINNET=true` and
`I_UNDERSTAND_THE_RISK=yes`. New orders are **disabled** unless
`ENABLE_TRADING=true` is set.

```bash
# 1. Clone
git clone <your-repo-url> trading-engine
cd trading-engine

# 2. Create environment from the template (never commit .env)
cp .env.example .env
# Edit .env: fill BYBIT_API_KEY / BYBIT_API_SECRET for your Bybit DEMO subaccount.
# Leave them blank to run without any venue connection.

# 3. Install dependencies
python -m pip install -r requirements.txt
python -m pip install pytest ruff

# 4. Run the engine on demo (trading disabled by default — no orders placed)
python3 live/agent_runner.py

# To allow new orders on demo:
#   ENABLE_TRADING=true python3 live/agent_runner.py
```

`.env.example` documents every variable: venue (`BYBIT_DEMO=true`), the
mainnet interlock (`ALLOW_MAINNET`, `I_UNDERSTAND_THE_RISK`), the trading opt-in
(`ENABLE_TRADING`), risk dials, and optional alerts (ntfy, Discord, SMTP) — all
read from env, none hardcoded.

---

## Data download (`scripts/download_data.py`)

`data/download_data.py` pulls 3+ years of public mainnet candles + funding from
**Bybit public REST v5 (no API key)**:

```
python data/download_data.py
```

- Deterministic daily chunks, resumable (existing chunks skipped), retry with
  backoff on Bybit rate limits.
- Writes per-chunk CSVs, a SHA-256 manifest (`data/checksums.sha256`), a cutoff
  record (`data/cutoff.txt`), and a gap report (`data/gap_report.txt`).
- Symbols/timeframes: BTC/USDT, ETH/USDT at 5m, 15m, 1d (see `research/PREREG.md`
  §Data). The CSV files themselves are large and are **not** part of the
  repository (`data/*.csv` is gitignored); `data/checksums.sha256`,
  `data/cutoff.txt` and `data/gap_report.txt` are kept.

The checkout ships with `data/checksums.sha256`, `data/cutoff.txt`,
`data/gap_report.txt`, and `data/download_data.py` only — no raw candles.

---

## Tests and CI

```bash
# Full suite
python -m pytest tests/ -v

# Lint (CI-enforced rules: syntax / undefined names / unused imports)
ruff check --select=E9,F63,F7,F82 .

# Secret scan (pre-commit runs this on every commit)
gitleaks detect --source . --no-git --verbose
```

CI (`.github/workflows/ci.yml`) runs on every push/PR:
1. `gitleaks` secret scan on the tree.
2. `pytest tests/` test suite.
3. `pip-audit` against pinned `requirements.txt` (findings are reported, not
   hidden).
4. `ruff` lint.
5. Module-import verification.

---

## Limitations

- Defaults to Bybit demo/sandbox; mainnet is refused unless both consent flags
  are set, and even then you trade real money at your own risk.
- Pandas-ta / numba-based indicators require Python < 3.14 (CI uses 3.12); on
  Python 3.14 the modules still import but trailing-stop math needs pandas_ta.
- The replay harness makes deterministic conventions (intrabar fills, mark
  vs last price) documented as deliberate deviations in `research/SEMANTICS.md`
  §8; it is an approximation of live execution, not a guarantee.
- No strategy is guaranteed to be profitable; several were removed after
  walk-forward showed no edge (see Research honesty below).

---

## Research honesty

Strategies **S1, S3, S4, and S5** were disabled after walk-forward showed no
edge against the preregistered eligibility rules in `research/PREREG.md`
(E1–E7: net expectancy ≥ +0.15 R, bootstrap lower bound > 0, net profit factor
≥ 1.30, random-entry p < 0.05, positive in ≥ 3 of 4 rolling OOS windows, max
drawdown ≤ 15 %, robust to cost stress). None cleared the gate, so they are
registered in `STRATEGY_CONFIG` but excluded from `ACTIVE_STRATEGIES`.

Strategy **S9 ("sacred harmonic")** — a numerology-based scored strategy — has
been removed entirely from this public copy (code, config, registry, tests, and
docs). If you need it restored, ask the maintainer; it is not part of the
public engine.

No profit, return, or win-rate claims are made anywhere in this repository.

---

## Demo assets (placeholders)

A short demo GIF and walkthrough video will be recorded and placed here:

- `docs/demo/demo.gif` — placeholder (to be recorded)
- `docs/demo/demo.mp4` — placeholder (to be recorded)

These do not exist yet; the links are reserved.

---

## License

[MIT](LICENSE) — Copyright (c) 2026 Hassnain Abbas.
