# P2 — Live verification of the survival mechanics

Static review confirmed the wiring (`risk_manager.update_balance` returns
`'lock'`/`'flatten'`; `engine.py` dispatches them; `can_open_trade` blocks new
entries when locked). This runbook proves it **live on Bybit demo** — the only
place exchange-integration bugs show up (see the project's standing rule: a fix
isn't done until a real order round-trips). **Must run on your machine**; the
sandbox can't reach the demo venue.

Goal: confirm four things.
1. S7 only ever enters **BTC and ETH**.
2. A real **IOC fill** lands (order placed → confirmed filled).
3. **−2%** session loss **pauses new entries** but lets open trades run to SL/TP.
4. **−4%** session loss **flattens** everything (and actually closes on the demo venue).

---

## 1. Launch (laptop)

Always via the loop wrapper — never bare `python live/agent_runner.py` for a long
run (the laptop's DNS watchdog wedges long-lived bare processes). The wrapper
activates the venv, runs `live/agent_runner.py`, and restarts on exit.

```bash
cd ~/Documents/crypto_agent
nohup bash deploy/run_agent_loop.sh > agent_run.log 2>&1 &
tail -f agent_run.log
```

- Single instance only: a second launch exits immediately (`agent.lock`, exit 3).
- Stop it: `pkill -f agent_runner.py` (the wrapper also stops when its child does).

---

## 2. Check: S7 enters only BTC/ETH

The placement log line looks like:

```
📋 IOC: buy 0.0004 BTC/USDT:USDT @ 65000.0000 (risk 0.8%, signal 65000.0000)
```

`risk 0.8%` is the S7 micro-risk fingerprint (S2 shows 1.5%, S9 ~1%), but the log
line doesn't name the strategy — the authoritative check is the DB, where
`signals.strategy` records the opener:

```bash
./venv/bin/python - <<'PY'
import sqlite3
from trading_phase import phase_started_dt
b = phase_started_dt().strftime('%Y-%m-%dT%H:%M:%S')
c = sqlite3.connect("agent_trades.db").cursor()
print("S7 symbols this phase:",
      c.execute("SELECT DISTINCT symbol FROM signals "
                "WHERE strategy='strategy7_daily_momentum' AND timestamp>=?", (b,)).fetchall())
PY
```

**PASS** = the set is a subset of `{BTC/USDT:USDT, ETH/USDT:USDT}`. Any alt = fail
(the STRATEGY_CONFIG cut didn't take — but it's verified in code, so this is the
belt-and-suspenders live confirmation).

---

## 3. Check: a real IOC fill

Sequence in the log for a genuine entry:

```
📋 IOC: buy 0.0004 BTC/USDT:USDT @ ...      # order placed (row status 'pending')
✅ Filled: buy 0.0004 BTC/USDT:USDT @ ...   # confirmed present in open positions
```

plus an ntfy trade-entry alert. **PASS** = you see a `✅ Filled` line. If instead
you see `Refusing … IOC: book price … deviates` or `Cancelled unfilled limit …`,
that's the demo order-book defect, not a real fill — note the symbol and move on.

```bash
grep -E "📋|✅ Filled|Refusing|Cancelled unfilled" agent_run.log | tail -20
```

---

## 4 & 5. Check: −2% lock and −4% flatten

### Passive (if a losing day happens naturally)
At −2% of session-start equity you'll see (log + ntfy):

```
Daily loss lock (−2%): session P&L -1.00 USDT. New entries paused for the rest
of the UTC day; open positions still run to SL/TP.
```

After that, **no new `📋` lines** appear until the next UTC day, but any already-open
position still closes normally via its own SL/TP. At −4%:

```
Daily −4% hit — flattening all positions.
Daily loss halt: flattened N position(s) ...
```

and every open position is market-closed (its `signals` row becomes
`status='closed', exit_reason='daily_loss_flatten'`).

### Active test (recommended — don't wait for a bad day)
The thresholds are now env-overridable (defaults 0.02/0.04 unchanged). Set them
tiny so normal noise trips them within a poll cycle. **Demo money — safe.**

```bash
pkill -f agent_runner.py
# lock at 0.1% of equity (~$0.05), flatten at 0.2% (~$0.10)
DAILY_LOSS_LOCK_PCT=0.001 DAILY_FLATTEN_PCT=0.002 \
  nohup bash deploy/run_agent_loop.sh > agent_run.log 2>&1 &
tail -f agent_run.log
```

Confirm, in order:
- **Lock fires**: the `Daily loss lock (−2%)` alert appears (the label says −2%
  even though you tightened it — it's the same code path).
- **Entries pause**: no new `📋` lines after the lock.
- **Open trades survive the lock**: a position open at lock time is *not* force-closed
  by the lock — only by its own SL/TP.
- **Flatten fires**: once P&L crosses the flatten line, the `Daily −4% hit` log +
  `flattened N position(s)` ntfy appear.
- **Flatten actually closes on the venue** (THE critical check — this is the
  exchange-integration step static review can't prove): open the Bybit demo
  positions page and confirm positions are gone; and in the DB:

```bash
./venv/bin/python - <<'PY'
import sqlite3
c = sqlite3.connect("agent_trades.db").cursor()
print("flatten-closed rows:",
      c.execute("SELECT COUNT(*) FROM signals WHERE exit_reason='daily_loss_flatten'").fetchone()[0])
PY
```

### Restore defaults after the test
```bash
pkill -f agent_runner.py
nohup bash deploy/run_agent_loop.sh > agent_run.log 2>&1 &   # no env overrides = 2%/4%
```

Note in the phase journal that any `daily_loss_flatten` rows from this test are
**test artifacts**, so they aren't later mistaken for real strategy losses.

---

## Pass/fail summary

| # | Mechanic | PASS signal |
|---|----------|-------------|
| 1 | S7 universe | `signals.strategy='strategy7_daily_momentum'` symbols ⊆ {BTC, ETH} |
| 2 | IOC fill | `✅ Filled:` line + entry ntfy |
| 3 | −2% lock | lock alert; no new `📋` after; open trade still runs to SL/TP |
| 4 | −4% flatten | flatten log + ntfy; positions gone on Bybit; `daily_loss_flatten` rows written |

Only when 3 and 4 pass **live** is the capital-preservation net trustworthy for
the eventual real-money switch.
