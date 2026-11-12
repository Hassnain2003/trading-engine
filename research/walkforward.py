"""Stage 3 walk-forward research/backtest harness.

Faithfully reproduces live ETHUSDT (and BTCUSDT) execution semantics on the
validated three-year dataset (2023-10-20 → 2026-10-19). Replays the live
engine bar-by-bar through ReplayBroker, then reports PREREG eligibility
metrics for the four out-of-sample windows.

Run:
    venv/bin/python -m research.walkforward [--symbols ETH,BTC] [--equity 500]
                                            [--slippage 3] [--cap-mode code|intent]
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone

import pandas as pd

from research.data_loader import (
    TIMEFRAME_MS, load_ohlcv, load_funding, SYMBOL_FILE_KEY,
)
from research.replay_exchange import ReplayExchange
from research.replay_broker import ReplayBroker

# ─── OOS windows (PREREG E5) ────────────────────────────────────────────
WINDOWS = [
    ('W1', '2025-10-21', '2026-01-21'),
    ('W2', '2026-01-21', '2026-04-21'),
    ('W3', '2026-04-21', '2026-07-21'),
    ('W4', '2026-07-21', '2026-10-19'),
]

DATA_START = '2023-10-20T21:05:00+00:00'


def _symbols_arg(s: str):
    """Parse --symbols argument like 'ETH' or 'ETH,BTC' into ccxt symbols."""
    out = []
    for part in s.split(','):
        part = part.strip().upper()
        if part == 'ETH':
            out.append('ETH/USDT:USDT')
        elif part == 'BTC':
            out.append('BTC/USDT:USDT')
        else:
            out.append(part)
    return out


def _bars_df(symbol, timeframe='5m'):
    """Load the full contiguous OHLCV frame for a symbol/timeframe."""
    return load_ohlcv(symbol, timeframe)


def run_walkforward(symbols=('ETH/USDT:USDT',),
                    starting_equity=500.0, slippage_bps=3.0,
                    cap_mode='code', verbose=True):
    """Run the full 3-year walk-forward replay.

    Parameters
    ----------
    symbols : tuple[str]
        CCXT symbols to replay (default ETH only; add BTC for full contention).
    starting_equity : float
        Starting cash equity in USDT (micro phase = 500).
    slippage_bps : float
        Slippage per side in basis points (preregistered = 3).
    cap_mode : str
        'code' = reproduce live code semantics exactly (3 evaluations/strategy/day,
        regardless of entries). 'intent' = count actual entries toward the 3/day cap.
    verbose : bool
        Print progress.
    """
    # Reference symbol = BTC 5m (live engine's bar reference)
    ref_symbol = 'BTC/USDT:USDT'
    ref_df = _bars_df(ref_symbol, '5m')

    # Determine time range from the reference data
    start_ts = pd.Timestamp(DATA_START)
    end_ts = ref_df.index[-1]

    if verbose:
        print(f'=== Stage 3 Walk-Forward Replay ===')
        print(f'Symbols: {symbols}')
        print(f'Reference: {ref_symbol} 5m')
        print(f'Range: {start_ts} → {end_ts}')
        print(f'Starting equity: ${starting_equity:,.2f}')
        print(f'Slippage: {slippage_bps} bps/side | Cap mode: {cap_mode}')
        print()

    # Initialise exchange + broker
    exchange = ReplayExchange(start_ts, list(symbols) + [ref_symbol],
                              synth_forming=True)
    broker = ReplayBroker(exchange, starting_equity=starting_equity,
                          slippage_bps=slippage_bps, cap_mode=cap_mode)

    # Iterate over every closed 5m bar of the reference symbol
    ref_bars = ref_df.loc[start_ts:end_ts]
    n_bars = len(ref_bars)
    if verbose:
        print(f'Replaying {n_bars:,} 5m bars...')

    for i, (ts, row) in enumerate(ref_bars.iterrows()):
        # Build bar dict for each symbol at this timestamp
        bars_5m = {}
        for sym in symbols:
            try:
                df = _bars_df(sym, '5m')
                if ts in df.index:
                    r = df.loc[ts]
                    bars_5m[sym] = {
                        'open': float(r['open']), 'high': float(r['high']),
                        'low': float(r['low']), 'close': float(r['close']),
                        'volume': float(r['volume']),
                    }
            except Exception:
                pass
        # Reference bar (always present)
        bars_5m[ref_symbol] = {
            'open': float(row['open']), 'high': float(row['high']),
            'low': float(row['low']), 'close': float(row['close']),
            'volume': float(row['volume']),
        }

        broker.step(ts.to_pydatetime(), bars_5m)

        if verbose and (i + 1) % 50000 == 0:
            equity = broker._equity({sym: b['close'] for sym, b in bars_5m.items()})
            n_pos = len(broker.positions)
            n_trades = len(broker.trades)
            print(f'  Bar {i+1:,}/{n_bars:,} | equity=${equity:,.2f} | '
                  f'positions={n_pos} | trades={n_trades}')

    if verbose:
        print(f'\nReplay complete. {len(broker.trades)} trades, '
              f'{len(broker.funding_payments)} funding payments.')

    return broker


def _trades_in_window(trades, start, end):
    """Filter trades whose exit time falls within [start, end)."""
    start_ts = pd.Timestamp(start, tz='UTC')
    end_ts = pd.Timestamp(end, tz='UTC')
    return [t for t in trades
            if start_ts <= pd.Timestamp(t.exit_time, tz='UTC') < end_ts]


def compute_window_metrics(trades, starting_equity):
    """Compute PREREG metrics for a list of trades."""
    if not trades:
        return {
            'n_trades': 0, 'net_pnl': 0.0, 'gross_profit': 0.0,
            'gross_loss': 0.0, 'profit_factor': 0.0,
            'win_rate': 0.0, 'expectancy_R': 0.0,
            'max_drawdown': 0.0, 'avg_R': 0.0,
        }

    pnls = [t.pnl_net for t in trades]
    gross_profit = sum(p for p in pnls if p > 0)
    gross_loss = sum(-p for p in pnls if p < 0)
    net_pnl = sum(pnls)
    n_wins = sum(1 for p in pnls if p > 0)
    n_losses = sum(1 for p in pnls if p < 0)
    n_trades = len(trades)

    profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else float('inf')
    win_rate = n_wins / n_trades if n_trades > 0 else 0.0

    # Expectancy in R
    r_vals = [t.r_multiple for t in trades if t.r_multiple is not None]
    expectancy_R = sum(r_vals) / len(r_vals) if r_vals else 0.0

    # Max drawdown on equity curve
    equity = starting_equity
    peak = starting_equity
    max_dd = 0.0
    for t in trades:
        equity += t.pnl_net
        if equity > peak:
            peak = equity
        dd = (peak - equity) / peak
        if dd > max_dd:
            max_dd = dd

    return {
        'n_trades': n_trades,
        'net_pnl': net_pnl,
        'gross_profit': gross_profit,
        'gross_loss': gross_loss,
        'profit_factor': profit_factor,
        'win_rate': win_rate,
        'expectancy_R': expectancy_R,
        'max_drawdown': max_dd,
        'avg_R': expectancy_R,
    }


def report(broker, starting_equity=500.0, verbose=True):
    """Report per-window metrics and PREREG eligibility."""
    trades = broker.trades
    print('\n=== Per-Window Metrics (OOS) ===')
    print(f'{"Window":<8} {"Period":<24} {"Trades":<7} {"Net P&L":<12} '
          f'{"PF":<7} {"Win%":<7} {"Exp(R)":<8} {"MaxDD":<8}')
    window_results = {}
    for wname, wstart, wend in WINDOWS:
        wtrades = _trades_in_window(trades, wstart, wend)
        m = compute_window_metrics(wtrades, starting_equity)
        window_results[wname] = m
        period = f'{wstart} → {wend}'
        print(f'{wname:<8} {period:<24} {m["n_trades"]:<7} '
              f'${m["net_pnl"]:<11,.2f} {m["profit_factor"]:<7.2f} '
              f'{m["win_rate"]*100:<6.1f}% {m["expectancy_R"]:<8.3f} '
              f'{m["max_drawdown"]*100:<7.2f}%')

    # Overall
    m_all = compute_window_metrics(trades, starting_equity)
    print(f'{"ALL":<8} {"2023-10-20 → 2026-10-19":<24} '
          f'{m_all["n_trades"]:<7} ${m_all["net_pnl"]:<11,.2f} '
          f'{m_all["profit_factor"]:<7.2f} {m_all["win_rate"]*100:<6.1f}% '
          f'{m_all["expectancy_R"]:<8.3f} {m_all["max_drawdown"]*100:<7.2f}%')

    # PREREG eligibility
    print('\n=== PREREG Eligibility (E1-E7) ===')
    pos_windows = sum(1 for m in window_results.values() if m['net_pnl'] > 0)
    all_m = compute_window_metrics(trades, starting_equity)

    checks = {
        'E1 Net expectancy ≥ +0.15 R':
            all_m['expectancy_R'] >= 0.15,
        'E3 Net profit factor ≥ 1.30':
            all_m['profit_factor'] >= 1.30,
        'E5 Positive in ≥ 3 of 4 OOS windows':
            pos_windows >= 3,
        'E6 Max drawdown ≤ 15%':
            all_m['max_drawdown'] <= 0.15,
    }
    for name, result in checks.items():
        print(f'  {"PASS" if result else "FAIL"}  {name}')
    print(f'  (E2 bootstrap CI and E4 random-entry p-value require resampling; '
          f'see tests/test_semantics.py)')
    print(f'  E7 cost-stress: run with higher slippage/fees for the ×1.5/×2 test')

    return window_results


def main():
    parser = argparse.ArgumentParser(description='Stage 3 walk-forward replay')
    parser.add_argument('--symbols', type=_symbols_arg, default='ETH',
                        help='Comma-separated: ETH, BTC')
    parser.add_argument('--equity', type=float, default=500.0,
                        help='Starting equity in USDT')
    parser.add_argument('--slippage', type=float, default=3.0,
                        help='Slippage per side in bps (preregistered = 3)')
    parser.add_argument('--fees', type=float, default=0.55,
                        help='Taker fee per side in bps (live = 0.55)')
    parser.add_argument('--cap-mode', type=str, default='code',
                        choices=['code', 'intent'],
                        help='Entry-cap semantics: code (live) or intent')
    parser.add_argument('--quiet', action='store_true',
                        help='Suppress progress output')
    args = parser.parse_args()

    broker = run_walkforward(
        symbols=tuple(args.symbols),
        starting_equity=args.equity,
        slippage_bps=args.slippage,
        cap_mode=args.cap_mode,
        verbose=not args.quiet,
    )
    report(broker, starting_equity=args.equity, verbose=not args.quiet)
    return broker


if __name__ == '__main__':
    main()