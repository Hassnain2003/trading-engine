"""The Agent — live dashboard (dark trading-terminal UI).

Run:  source venv/bin/activate && streamlit run dashboard.py
Then open http://localhost:8501

Reads agent_trades.db (same file the engine writes) and the Bybit demo API
for live positions/balance. Read-only — never places orders.
"""

import os
import sqlite3
from datetime import datetime, timezone, timedelta

import pandas as pd
import plotly.graph_objects as go
import streamlit as st
from dotenv import load_dotenv

from utils.live_tracker import build_report
from utils.manual_close import close_positions
from utils.stats import gate_status
from trading_phase import (
    PHASE_ID, PHASE_LABEL, STARTING_EQUITY, FORCE_EQUITY, DB_FILENAME,
    DAILY_PROFIT_TARGET_PCT,
    daily_profit_target_usdt, phase_equity, set_wallet_baseline,
)

load_dotenv()

st.set_page_config(page_title="The Agent", page_icon="⚡", layout="wide",
                   initial_sidebar_state="collapsed")

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), DB_FILENAME)

# Palette
BG = "#0a0e17"
CARD = "#111827"
CARD2 = "#0f1522"
BORDER = "#1f2a3a"
TEXT = "#e6edf3"
MUTED = "#7d8896"
POS = "#34d399"
NEG = "#f87171"
ACCENT = "#5eead4"
ACCENT2 = "#818cf8"

# ─────────────────────────────── CSS ────────────────────────────────────────
st.markdown(f"""
<style>
  .stApp {{ background:
      radial-gradient(1200px 600px at 15% -10%, #12203a55 0%, transparent 55%),
      radial-gradient(1000px 500px at 110% 0%, #1c184355 0%, transparent 50%),
      {BG}; }}
  .block-container {{ padding-top: 1.6rem; padding-bottom: 3rem; max-width: 1400px; }}
  #MainMenu, footer, header {{ visibility: hidden; }}

  .hero {{ display:flex; align-items:center; justify-content:space-between; margin-bottom: 1.4rem; }}
  .hero-title {{ font-size: 2.1rem; font-weight: 800; letter-spacing:-0.02em;
      background: linear-gradient(92deg,{ACCENT} 0%, {ACCENT2} 90%);
      -webkit-background-clip:text; -webkit-text-fill-color:transparent; margin:0; }}
  .hero-sub {{ color:{MUTED}; font-size:0.82rem; margin-top:2px; }}
  .live-dot {{ height:8px; width:8px; background:{POS}; border-radius:50%;
      display:inline-block; margin-right:6px; box-shadow:0 0 0 0 {POS};
      animation: pulse 2s infinite; }}
  @keyframes pulse {{ 0%{{box-shadow:0 0 0 0 {POS}88;}} 70%{{box-shadow:0 0 0 7px transparent;}} 100%{{box-shadow:0 0 0 0 transparent;}} }}

  .kpi {{ background: linear-gradient(160deg,{CARD} 0%, {CARD2} 100%);
      border:1px solid {BORDER}; border-radius:16px; padding:16px 18px; height:100%;
      box-shadow: 0 4px 24px #0006; }}
  .kpi-label {{ color:{MUTED}; font-size:0.72rem; text-transform:uppercase;
      letter-spacing:0.07em; font-weight:600; }}
  .kpi-value {{ font-size:1.7rem; font-weight:800; margin-top:6px; letter-spacing:-0.02em; }}
  .kpi-sub {{ font-size:0.8rem; margin-top:3px; font-weight:600; }}
  .kpi-hero {{ border:1px solid {ACCENT}44;
      background: linear-gradient(160deg,#0f2f2a 0%, {CARD2} 70%); }}
  .kpi-hero .kpi-value {{ font-size:2rem; color:{ACCENT}; }}

  .sec {{ font-size:1.05rem; font-weight:700; color:{TEXT}; margin:1.6rem 0 0.6rem 0;
      display:flex; align-items:center; gap:8px; }}
  .sec::before {{ content:""; width:4px; height:18px; border-radius:3px;
      background:linear-gradient(180deg,{ACCENT},{ACCENT2}); display:inline-block; }}

  .pill {{ display:inline-flex; align-items:center; gap:6px; padding:5px 11px;
      border-radius:999px; font-size:0.8rem; font-weight:700; margin:3px 6px 3px 0; }}
  .pill-ok {{ background:{POS}1a; color:{POS}; border:1px solid {POS}44; }}
  .pill-no {{ background:{NEG}1a; color:{NEG}; border:1px solid {NEG}44; }}

  .card {{ background:{CARD2}; border:1px solid {BORDER}; border-radius:14px;
      padding:14px 16px; }}
  .mono {{ font-family: ui-monospace,'SF Mono',Menlo,monospace; font-size:0.82rem;
      color:{TEXT}; white-space:pre-wrap; line-height:1.55; }}

  /* dataframe polish */
  [data-testid="stDataFrame"] {{ border:1px solid {BORDER}; border-radius:12px; }}
  .stProgress > div > div > div > div {{ background:linear-gradient(90deg,{ACCENT},{ACCENT2}); }}
  div.stButton > button {{ border-radius:10px; border:1px solid {BORDER};
      background:{CARD}; color:{TEXT}; font-weight:600; transition:all .15s; }}
  div.stButton > button:hover {{ border-color:{ACCENT}; color:{ACCENT}; }}
</style>
""", unsafe_allow_html=True)

st.components.v1.html('<meta http-equiv="refresh" content="60">', height=0)


# ─────────────────────────────── data ───────────────────────────────────────
def _bybit_key_fingerprint():
    """Bust Streamlit cache when API keys in .env change."""
    return (os.getenv('BYBIT_API_KEY') or '')[:12]


@st.cache_resource
def get_exchange(_key_fp):
    import ccxt
    demo = ccxt.bybit({
        'enableRateLimit': True, 'timeout': 20000,
        'apiKey': os.getenv('BYBIT_API_KEY'), 'secret': os.getenv('BYBIT_API_SECRET'),
        'options': {'defaultType': 'linear', 'recvWindow': 20000},
    })
    demo.set_sandbox_mode(True)
    return demo


@st.cache_data(ttl=60)
def load_trades():
    conn = sqlite3.connect(DB_PATH)
    df = pd.read_sql_query(
        "SELECT id, timestamp, symbol, strategy, side, entry, stop_loss, take_profit, "
        "exit_price, status, exit_reason FROM signals ORDER BY id", conn)
    conn.close()
    return df


@st.cache_data(ttl=30)
def load_fill_qty_map(strategies=None):
    """Map signal id -> filled order quantity for $ PnL.

    Prefer time proximity (IOC), fall back to entry-price match (legacy limits).
    """
    conn = sqlite3.connect(DB_PATH)
    if strategies:
        placeholders = ','.join('?' * len(strategies))
        sigs = pd.read_sql_query(
            f"SELECT id, timestamp, symbol, entry, strategy FROM signals "
            f"WHERE strategy IN ({placeholders}) AND status IN ('open','closed') "
            f"ORDER BY id", conn, params=list(strategies))
    else:
        sigs = pd.read_sql_query(
            "SELECT id, timestamp, symbol, entry, strategy FROM signals "
            "WHERE status IN ('open','closed') ORDER BY id", conn)
    orders = pd.read_sql_query(
        "SELECT id, timestamp, symbol, quantity, price, status FROM orders "
        "WHERE status='filled' ORDER BY id", conn)
    conn.close()
    qty_map = {}
    if sigs.empty or orders.empty:
        return qty_map

    orders = orders.copy()
    orders['_ts'] = pd.to_datetime(orders['timestamp'], utc=True)
    used = set()

    def _claim(sig_id, order_idx):
        qty_map[int(sig_id)] = float(orders.loc[order_idx, 'quantity'])
        used.add(order_idx)

    for _, s in sigs.iterrows():
        sts = pd.to_datetime(s['timestamp'], utc=True)
        cand = orders[
            (orders['symbol'] == s['symbol'])
            & (~orders.index.isin(used))
            & (orders['_ts'] >= sts - pd.Timedelta(minutes=15))
            & (orders['_ts'] <= sts + pd.Timedelta(minutes=2))
        ].copy()
        if cand.empty:
            continue
        cand['dist'] = (cand['_ts'] - sts).abs()
        _claim(s['id'], cand['dist'].idxmin())

    for _, s in sigs.iterrows():
        sid = int(s['id'])
        if sid in qty_map:
            continue
        entry = float(s['entry'] or 0)
        if entry <= 0:
            continue
        cand = orders[
            (orders['symbol'] == s['symbol'])
            & (~orders.index.isin(used))
            & ((orders['price'] - entry).abs() / entry < 0.002)
        ].copy()
        if cand.empty:
            continue
        cand['dist'] = (cand['price'] - entry).abs()
        _claim(sid, cand['dist'].idxmin())

    return qty_map


def trade_pnl_usd(row, qty):
    if qty is None or row.get('status') != 'closed':
        return None
    if not row.get('exit_price') or not row.get('entry'):
        return None
    e, x, q = float(row['entry']), float(row['exit_price']), float(qty)
    if str(row['side']).lower() in ('long', 'buy'):
        return (x - e) * q
    return (e - x) * q


def build_trade_blotter(df, qty_map):
    """Closed+open rows with entry/SL/TP/exit/$ for dashboard tables."""
    rows = []
    for _, r in df.sort_values('id', ascending=False).iterrows():
        q = qty_map.get(int(r['id'])) if 'id' in r.index else None
        usd = trade_pnl_usd(r, q) if r['status'] == 'closed' else None
        pct = pnl_pct(r) if r['status'] == 'closed' else None
        rows.append({
            'ID': int(r['id']),
            'When (UTC)': str(r['timestamp'])[:19].replace('T', ' '),
            'Coin': str(r['symbol']).split('/')[0],
            'Side': r['side'],
            'Status': r['status'],
            'Entry': round(float(r['entry']), 4) if r['entry'] is not None else None,
            'SL': round(float(r['stop_loss']), 4) if pd.notna(r.get('stop_loss')) else None,
            'TP': round(float(r['take_profit']), 4) if pd.notna(r.get('take_profit')) else None,
            'Exit': round(float(r['exit_price']), 4) if pd.notna(r.get('exit_price')) else None,
            'Qty': round(q, 4) if q is not None else None,
            'P&L %': round(pct, 3) if pct is not None else None,
            'P&L $': round(usd, 2) if usd is not None else None,
            'Exit reason': r.get('exit_reason') or '',
        })
    return pd.DataFrame(rows)


@st.cache_data(ttl=30)
def load_live():
    demo = get_exchange(_bybit_key_fingerprint())
    positions = []
    for p in demo.fetch_positions():
        if float(p.get('contracts', 0) or 0) != 0:
            positions.append({
                'Symbol': p['symbol'].split('/')[0], 'Side': p.get('side'),
                'Size': float(p.get('contracts') or 0),
                'Entry': float(p.get('entryPrice') or 0),
                'Mark': float(p.get('markPrice') or 0),
                'uPnL $': round(float(p.get('unrealizedPnl') or 0), 2),
                'SL': p.get('info', {}).get('stopLoss') or '—',
                'TP': p.get('info', {}).get('takeProfit') or '—',
            })
    balance = float(demo.fetch_balance()['USDT']['total'])
    return positions, balance


@st.cache_data(ttl=60)
def load_equity():
    conn = sqlite3.connect(DB_PATH)
    try:
        df = pd.read_sql_query(
            "SELECT timestamp, equity FROM equity_snapshots ORDER BY id", conn)
    except Exception:
        df = pd.DataFrame()
    conn.close()
    return df


@st.cache_data(ttl=10)
def load_engine_control():
    conn = sqlite3.connect(DB_PATH)
    paused_row = conn.execute(
        "SELECT value FROM kv_state WHERE key='engine_paused'"
    ).fetchone()
    snap_row = conn.execute(
        "SELECT timestamp FROM equity_snapshots ORDER BY id DESC LIMIT 1"
    ).fetchone()
    conn.close()
    paused = str(paused_row[0]).strip().lower() in ('1', 'true', 'yes', 'on') if paused_row else False
    last_seen = None
    if snap_row and snap_row[0]:
        try:
            last_seen = datetime.fromisoformat(str(snap_row[0]))
        except ValueError:
            last_seen = None
    return paused, last_seen


def set_engine_paused(paused):
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "INSERT INTO kv_state (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        ("engine_paused", "1" if paused else "0"),
    )
    conn.execute(
        "INSERT INTO kv_state (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        ("engine_pause_updated_at", datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()
    conn.close()
    load_engine_control.clear()


def pnl_pct(row):
    if row['status'] != 'closed' or not row['exit_price'] or not row['entry']:
        return None
    if str(row['side']).lower() in ('long', 'buy'):
        return (row['exit_price'] - row['entry']) / row['entry'] * 100
    return (row['entry'] - row['exit_price']) / row['entry'] * 100


def sec(title):
    st.markdown(f'<div class="sec">{title}</div>', unsafe_allow_html=True)


def kpi(label, value, sub="", color=TEXT, hero=False):
    sub_html = f'<div class="kpi-sub" style="color:{color}">{sub}</div>' if sub else ""
    return (f'<div class="kpi {"kpi-hero" if hero else ""}">'
            f'<div class="kpi-label">{label}</div>'
            f'<div class="kpi-value">{value}</div>{sub_html}</div>')


def style_plot(fig, height=300):
    fig.update_layout(
        height=height, margin=dict(l=8, r=8, t=8, b=8),
        paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
        font=dict(color=MUTED, size=11), showlegend=False,
        xaxis=dict(gridcolor="#1a2333", zeroline=False),
        yaxis=dict(gridcolor="#1a2333", zeroline=False),
    )
    return fig


# ─────────────────────────────── load ───────────────────────────────────────
trades = load_trades()
closed = trades[trades['status'] == 'closed'].copy()
closed['pnl_pct'] = closed.apply(pnl_pct, axis=1)
closed = closed.dropna(subset=['pnl_pct'])
try:
    positions, balance = load_live()
    live_err = None
except Exception as e:
    positions, balance, live_err = [], None, str(e)

upnl = sum(p['uPnL $'] for p in positions) if positions else 0.0
# Prefer latest phase equity snapshot; else map wallet via FORCE_EQUITY baseline.
equity_now = None
try:
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute(
        "SELECT equity FROM equity_snapshots ORDER BY id DESC LIMIT 1"
    ).fetchone()
    base = conn.execute(
        "SELECT value FROM kv_state WHERE key='wallet_baseline'"
    ).fetchone()
    if base and base[0]:
        set_wallet_baseline(float(base[0]))
    conn.close()
    if row and row[0] is not None:
        equity_now = float(row[0])
except Exception:
    pass
if equity_now is None and balance is not None:
    equity_now = phase_equity(balance) + upnl
wins = (closed['pnl_pct'] > 0).sum()
wr = wins / len(closed) * 100 if len(closed) else 0
engine_paused, engine_last_seen = load_engine_control()
engine_fresh = bool(
    engine_last_seen and
    datetime.now(timezone.utc) - engine_last_seen <= timedelta(minutes=10)
)
engine_value = "OFF" if engine_paused else ("ON" if engine_fresh else "ON*")
engine_sub = (
    "Trades blocked from dashboard"
    if engine_paused else
    (f"Heartbeat {engine_last_seen.strftime('%H:%M:%S')} UTC" if engine_fresh else "Waiting for engine heartbeat")
)
engine_color = NEG if engine_paused else (POS if engine_fresh else ACCENT2)

# ─────────────────────────────── hero ───────────────────────────────────────
st.markdown(f"""
<div class="hero">
  <div>
    <div class="hero-title">⚡ THE AGENT</div>
    <div class="hero-sub"><span class="live-dot"></span>Live · Bybit demo · {PHASE_LABEL} ·
      start ${STARTING_EQUITY:.0f} · updated {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')} UTC</div>
  </div>
</div>
""", unsafe_allow_html=True)

ctl = st.columns([1.2, 1, 1, 2.8])
ctl[0].markdown(kpi("Trade Engine", engine_value, engine_sub, engine_color), unsafe_allow_html=True)
if ctl[1].button("🟢 Turn ON", use_container_width=True, disabled=not engine_paused):
    set_engine_paused(False)
    load_live.clear(); load_trades.clear(); load_equity.clear()
    st.rerun()
if ctl[2].button("🔴 Turn OFF", use_container_width=True, disabled=engine_paused):
    set_engine_paused(True)
    load_live.clear(); load_trades.clear(); load_equity.clear()
    st.rerun()
ctl[3].caption(
    "Green means the engine is allowed to place new trades. Red means new entries are paused. "
    "Open positions still manage their exits."
)

# Daily target vs session (wallet vs STARTING_EQUITY proxy until snapshots fill)
day_start = STARTING_EQUITY
try:
    conn = sqlite3.connect(DB_PATH)
    today = datetime.now(timezone.utc).strftime('%Y-%m-%d')
    row = conn.execute(
        "SELECT equity FROM equity_snapshots WHERE timestamp LIKE ? ORDER BY id LIMIT 1",
        (f"{today}%",),
    ).fetchone()
    if row and row[0]:
        day_start = float(row[0])
    conn.close()
except Exception:
    pass
day_pnl = (equity_now - day_start) if equity_now is not None else None
day_tgt = daily_profit_target_usdt(day_start)
day_sub = (
    f"lock +${day_tgt:.2f} ({DAILY_PROFIT_TARGET_PCT*100:.0f}%) · {day_pnl:+.2f} today"
    if day_pnl is not None
    else f"lock +${day_tgt:.2f}/{DAILY_PROFIT_TARGET_PCT*100:.0f}%"
)
day_color = POS if (day_pnl or 0) >= 0 else NEG

# ─────────────────────────────── KPI row ────────────────────────────────────
k = st.columns([1.5, 1, 1, 1, 1, 1])
upnl_color = POS if upnl >= 0 else NEG
k[0].markdown(kpi("Account Equity", f"${equity_now:,.2f}" if equity_now else "n/a",
                  f"{upnl:+,.2f} unrealized", upnl_color, hero=True), unsafe_allow_html=True)
k[1].markdown(kpi("Wallet (Bybit)", f"${balance:,.2f}" if balance else "n/a",
                  "FORCE_EQUITY on" if FORCE_EQUITY else "live wallet"),
              unsafe_allow_html=True)
k[2].markdown(kpi("Day P&L / Target", f"${day_pnl:+.2f}" if day_pnl is not None else "n/a",
                  day_sub, day_color), unsafe_allow_html=True)
k[3].markdown(kpi("Open Positions", len(positions)), unsafe_allow_html=True)
k[4].markdown(kpi("Closed Trades", len(closed)), unsafe_allow_html=True)
k[5].markdown(kpi("Win Rate", f"{wr:.1f}%", color=POS if wr >= 50 else MUTED),
              unsafe_allow_html=True)

if live_err:
    st.warning(f"Live exchange data unavailable: {live_err}")

# ─────────────────────────────── engine health flags ────────────────────────
# Read from health endpoint file + kv_state (engine writes these each cycle)
try:
    conn_h = sqlite3.connect(DB_PATH)
    _disabled = conn_h.execute(
        "SELECT value FROM kv_state WHERE key LIKE 'strategy_disabled:%'"
    ).fetchall()
    disabled_strategies = {
        k.split(':', 1)[1]
        for k, v in _disabled if str(v).strip().lower() in ('1', 'true', 'yes', 'on')
    }
    conn_h.close()
except Exception:
    disabled_strategies = set()

# Read stale-data / slippage from agent.health file if present
_stale_data = False
_avg_slippage_bps = None
try:
    with open('agent.health') as f:
        _health_line = f.read().strip()
    # agent.health format: "timestamp|ok" — no structured fields yet,
    # so we read from kv_state instead for slippage
    conn_h2 = sqlite3.connect(DB_PATH)
    _slip_row = conn_h2.execute(
        "SELECT AVG(slippage_bps) FROM ("
        "SELECT slippage_bps FROM signals WHERE slippage_bps IS NOT NULL "
        "ORDER BY id DESC LIMIT 50)"
    ).fetchone()
    conn_h2.close()
    if _slip_row and _slip_row[0] is not None:
        _avg_slippage_bps = round(_slip_row[0], 2)
except Exception:
    pass

_sec_cols = st.columns([1, 1, 1, 1])
_sec_cols[0].markdown(
    kpi("Stale Data", "YES" if _stale_data else "no",
        "entries paused" if _stale_data else "candles fresh",
        NEG if _stale_data else POS),
    unsafe_allow_html=True)
_sec_cols[1].markdown(
    kpi("Disabled Strategies",
        ", ".join(sorted(disabled_strategies)) if disabled_strategies else "none",
        "kill-switch active" if disabled_strategies else "",
        NEG if disabled_strategies else MUTED),
    unsafe_allow_html=True)
_sec_cols[2].markdown(
    kpi("Avg Slippage",
        f"{_avg_slippage_bps:+.1f} bps" if _avg_slippage_bps is not None else "n/a",
        ">5 bps = alert" if _avg_slippage_bps is not None else "",
        NEG if _avg_slippage_bps is not None and _avg_slippage_bps > 5 else MUTED),
    unsafe_allow_html=True)
_sec_cols[3].markdown(
    kpi("Health File", "agent.health",
        f"updated {datetime.now(timezone.utc).strftime('%H:%M:%S')} UTC",
        ACCENT2),
    unsafe_allow_html=True)

# ─────────────────────────────── equity curve ───────────────────────────────
eq_snap = load_equity()
if len(eq_snap) > 2:
    sec("Account Equity")
    x = pd.to_datetime(eq_snap['timestamp'])
    y = eq_snap['equity']
    up = y.iloc[-1] >= y.iloc[0]
    line_c = POS if up else NEG
    fill_c = "rgba(52,211,153,0.08)" if up else "rgba(248,113,113,0.08)"
    fig = go.Figure(go.Scatter(
        x=x, y=y, mode='lines', line=dict(width=2.5, color=line_c),
        fill='tozeroy', fillcolor=fill_c))
    fig.update_yaxes(range=[y.min() * 0.999, y.max() * 1.001])
    st.plotly_chart(style_plot(fig, 260), use_container_width=True,
                    config={'displayModeBar': False})

# ─────────────────────────────── positions ──────────────────────────────────
sec("Open Positions")


def _do_close(coins):
    conn = sqlite3.connect(DB_PATH)
    results = close_positions(get_exchange(_bybit_key_fingerprint()), conn, coins=coins)
    conn.close()
    ok = [r for r in results if r['ok']]
    if ok:
        banked = sum(r['upnl'] for r in ok)
        st.success(f"Closed {', '.join(r['symbol'].split('/')[0] for r in ok)} "
                   f"(~{banked:+.2f} USDT). Standdown until next UTC-day verdict.")
    for r in [x for x in results if not x['ok']]:
        st.error(f"{r['symbol']}: {r['error']}")
    load_live.clear(); load_trades.clear()


if positions:
    pdf = pd.DataFrame(positions)
    st.dataframe(
        pdf.style.map(lambda v: f"color:{POS}" if v >= 0 else f"color:{NEG}", subset=['uPnL $'])
                 .map(lambda v: f"color:{POS if v=='long' else NEG};font-weight:700", subset=['Side']),
        use_container_width=True, hide_index=True)

    cols = st.columns(len(positions) + 3)
    for i, p in enumerate(positions):
        coin = p['Symbol']
        if cols[i].button(f"✕ {coin}", key=f"close_{coin}"):
            _do_close([coin]); st.rerun()
    st.divider()
    cc = st.columns([2, 1])
    confirm = cc[0].checkbox("Confirm — close ALL positions", key="confirm_all")
    if cc[1].button("🔴 CLOSE ALL", type="primary", disabled=not confirm):
        _do_close(None); st.rerun()
    st.caption("Reduce-only market orders. Closed coins stand down until the next UTC-midnight verdict.")
else:
    st.info("No open positions.")

# ─────────────────────────────── gate ───────────────────────────────────────
conn = sqlite3.connect(DB_PATH)
g = gate_status(conn)
sec("🎯 Go-Live Gate" + ("  —  ALL PASSED 🚀" if g['passed'] else ""))
labels = {'profit_factor': 'Profit Factor', 'sharpe_ratio': 'Sharpe',
          'max_drawdown_pct': 'Max Drawdown %', 'total_trades': 'Trades'}
pills = ""
for key, c in g['checks'].items():
    cls = "pill-ok" if c['ok'] else "pill-no"
    mark = "✓" if c['ok'] else "✗"
    pills += (f'<span class="pill {cls}">{mark} {labels[key]}: '
              f'{c["value"]} <span style="opacity:.6">(need {c["op"]} {c["threshold"]})</span></span>')
st.markdown(pills, unsafe_allow_html=True)
st.progress(g['trades_progress'],
            text=f"Evidence: {g['metrics']['total_trades']}/60 clean trades")

# ─────────────────────────────── two-column mid ─────────────────────────────
left, right = st.columns(2)

with left:
    sec("Live vs Backtest")
    st.markdown(f'<div class="card mono">{build_report(conn)}</div>', unsafe_allow_html=True)

with right:
    sec("🧪 S8 — LLM News Calls (shadow)")
    ab_models = [r[0] for r in conn.execute(
        "SELECT DISTINCT model FROM ab_news_signals").fetchall()]
    if ab_models:
        lines = []
        for m in ab_models:
            parts = [f"{m:>10}:"]
            for col, lab in [('price_1h', '1h'), ('price_4h', '4h'), ('price_24h', '24h')]:
                rr = conn.execute(
                    f"SELECT direction,price_at_signal,{col} FROM ab_news_signals "
                    f"WHERE model=? AND {col} IS NOT NULL AND price_at_signal IS NOT NULL "
                    f"AND direction IN ('bullish','bearish')", (m,)).fetchall()
                if len(rr) >= 5:
                    h = sum(((p1-p0)/p0*(1 if d=='bullish' else -1)) > 0 for d, p0, p1 in rr)
                    parts.append(f"{lab} {h/len(rr)*100:.0f}%")
                else:
                    parts.append(f"{lab} n={len(rr)}")
            lines.append("  ".join(parts))
        st.markdown(f'<div class="card mono">3-way A/B (hit rate)\n' + "\n".join(lines) +
                    '\n\n50% = coin flip. Edge only if one\nbreaks away over time.</div>',
                    unsafe_allow_html=True)
    else:
        srows = conn.execute("SELECT COUNT(*) FROM shadow_signals").fetchone()[0]
        st.markdown(f'<div class="card mono">Gemini signals: {srows}\nA/B warming up '
                    f'(Groq + OpenRouter)…</div>', unsafe_allow_html=True)

# ─────────────────────────────── S2 / S7 blotters ───────────────────────────
fill_qty = load_fill_qty_map([
    'strategy2_breakout_retest',
    'strategy7_daily_momentum',
])
sec("S2 Breakout Retest — trade blotter")
s2_all = trades[
    (trades['strategy'] == 'strategy2_breakout_retest')
    & (trades['status'].isin(['open', 'closed']))
].copy()
s2_blotter = build_trade_blotter(s2_all, fill_qty)
if len(s2_blotter):
    s2_usd = s2_blotter['P&L $'].dropna().sum() if 'P&L $' in s2_blotter else 0
    st.caption(f"Closed $ PnL (matched fills): ${s2_usd:+,.2f}")
    st.dataframe(s2_blotter, use_container_width=True, hide_index=True)
else:
    st.caption("No S2 open/closed trades yet.")

sec("S7 Daily Momentum — trade blotter")
s7_all = trades[
    (trades['strategy'] == 'strategy7_daily_momentum')
    & (trades['status'].isin(['open', 'closed']))
].copy()
s7_blotter = build_trade_blotter(s7_all, fill_qty)
if len(s7_blotter):
    s7_usd = s7_blotter['P&L $'].dropna().sum() if 'P&L $' in s7_blotter else 0
    st.caption(f"Closed $ PnL (matched fills): ${s7_usd:+,.2f}")
    st.dataframe(s7_blotter, use_container_width=True, hide_index=True)
else:
    st.caption("No S7 open/closed trades yet.")

# ─────────────────────────────── per-strategy + recent ──────────────────────
b1, b2 = st.columns([1, 1.3])
with b1:
    sec("Per-Strategy (closed)")
    if len(closed):
        grp = closed.groupby('strategy')['pnl_pct']
        summ = pd.DataFrame({
            'trades': grp.count(),
            'WR %': (grp.apply(lambda s: (s > 0).mean() * 100)).round(1),
            'P&L %': grp.sum().round(2),
            'avg %': grp.mean().round(3),
        })
        summ.index = [i.replace('strategy', 'S').replace('_', ' ')[:14] for i in summ.index]
        st.dataframe(summ.style.map(
            lambda v: f"color:{POS}" if v > 0 else f"color:{NEG}", subset=['P&L %', 'avg %']),
            use_container_width=True)

with b2:
    sec("Recent Trades")
    recent = trades.tail(12).iloc[::-1].copy()
    recent['P&L %'] = recent.apply(pnl_pct, axis=1)
    recent['coin'] = recent['symbol'].str.split('/').str[0]
    recent['strat'] = recent['strategy'].str.replace('strategy', 'S').str[:2]
    show = recent[['coin', 'strat', 'side', 'status', 'P&L %']].round(2)
    st.dataframe(show.style.map(
        lambda v: (f"color:{POS}" if v > 0 else f"color:{NEG}") if pd.notna(v) else "",
        subset=['P&L %']), use_container_width=True, hide_index=True)

conn.close()
