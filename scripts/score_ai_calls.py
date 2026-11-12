#!/usr/bin/env python3
"""Score the AI layer's calls against what the market actually did.

    python scripts/score_ai_calls.py

Two datasets:
  1. shadow_signals — Gemini's news-based directional calls (1h/4h/24h).
  2. ai_recommendations — pre-session ACTIVE/REDUCED/DISABLED statuses:
     did REDUCED/DISABLED days actually turn out worse for that coin?

This is the evidence base for the (future, gated) conviction-sizing feature:
the AI only earns sizing influence if these numbers say its calls carry
information.
"""

import os
import sys
import sqlite3
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from trading_phase import DB_FILENAME
DB = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), DB_FILENAME)


def score_shadow(conn):
    print("=== Shadow news signals (Gemini direction vs realized price) ===")
    for col, label in [('price_1h', '1h'), ('price_4h', '4h'), ('price_24h', '24h')]:
        rows = conn.execute(
            f"""SELECT direction, severity, price_at_signal, {col} FROM shadow_signals
                WHERE {col} IS NOT NULL AND price_at_signal IS NOT NULL
                AND direction IN ('bullish','bearish')"""
        ).fetchall()
        if len(rows) < 5:
            print(f"  [{label}] insufficient matured directional signals ({len(rows)})")
            continue
        n, hits, ret_sum = 0, 0, 0.0
        hi_n, hi_hits = 0, 0
        for direction, sev, p0, p1 in rows:
            r = (p1 - p0) / p0 * (1 if direction == 'bullish' else -1)
            n += 1; hits += r > 0; ret_sum += r
            if sev and sev >= 3:
                hi_n += 1; hi_hits += r > 0
        line = f"  [{label}] n={n}  hit={hits/n*100:.1f}%  avg signed={ret_sum/n*1e4:+.1f} bps"
        if hi_n:
            line += f"  | severity>=3: n={hi_n} hit={hi_hits/hi_n*100:.1f}%"
        print(line)


def score_recommendations(conn):
    print("\n=== Pre-session statuses (did REDUCED/DISABLED coins have worse days?) ===")
    rows = conn.execute(
        "SELECT timestamp, coin, status FROM ai_recommendations ORDER BY id"
    ).fetchall()
    if not rows:
        print("  no recommendations logged yet")
        return
    by_status = {}
    for ts, coin, status in rows:
        day = ts[:10]
        # next-day (same-day-after-call) return from equity of that coin isn't
        # stored; use shadow price columns when the coin matches, else skip.
        by_status.setdefault(status, 0)
        by_status[status] += 1
    total = sum(by_status.values())
    print(f"  logged: {total} calls — " + ", ".join(f"{k}: {v}" for k, v in sorted(by_status.items())))
    non_active = total - by_status.get('ACTIVE', 0)
    if non_active < 5:
        print("  not enough non-ACTIVE calls to score discrimination yet — keep collecting.")


def score_ab(conn):
    print("\n=== A/B: Gemini vs Groq on the SAME headlines ===")
    models = [r[0] for r in conn.execute("SELECT DISTINCT model FROM ab_news_signals").fetchall()]
    if not models:
        print("  no A/B data yet (add GROQ_API_KEY to .env to activate the head-to-head)")
        return
    for model in models:
        parts = [f"  {model}:"]
        for col, label in [('price_1h', '1h'), ('price_4h', '4h'), ('price_24h', '24h')]:
            rows = conn.execute(
                f"""SELECT direction, price_at_signal, {col} FROM ab_news_signals
                    WHERE model=? AND {col} IS NOT NULL AND price_at_signal IS NOT NULL
                    AND direction IN ('bullish','bearish')""", (model,)
            ).fetchall()
            if len(rows) >= 5:
                hits = sum(((p1 - p0) / p0 * (1 if d == 'bullish' else -1)) > 0 for d, p0, p1 in rows)
                parts.append(f"[{label}] {hits}/{len(rows)}={hits/len(rows)*100:.0f}%")
            else:
                parts.append(f"[{label}] n={len(rows)}")
        print(" ".join(parts))


if __name__ == "__main__":
    conn = sqlite3.connect(DB)
    score_shadow(conn)
    score_ab(conn)
    score_recommendations(conn)
