#!/usr/bin/env python
"""backtest_scalp.py — intraday scalp evaluation with honest costs.

backtest.py models a long/flat daily strategy. A scalp needs more: intrabar
stops and targets, a holding-time cap, regular-hours-only trading, flat by the
close, and a spread charged on both sides. Reporting return alone would be
meaningless — the useful numbers are edge per trade in basis points against the
cost of getting in and out.

  python backtest_scalp.py scalp_vwap SPY
  python backtest_scalp.py scalp_vwap SPY --target-bp 10 --stop-bp 8 --spread-bp 0.13

Assumptions stated plainly, because they decide the answer:
  * Entry fills at the NEXT bar's open plus half the spread (never the signal
    bar's close — that is lookahead).
  * Stops and targets are checked against the bar's high/low. When a bar spans
    both, the STOP is assumed to hit first. That is the pessimistic ordering and
    the honest one, since a minute bar cannot say which came first.
  * Slippage beyond the quoted spread is NOT modeled, so real results would be
    worse than these, not better.
"""

from __future__ import annotations

import argparse
import csv
import importlib
import math
import statistics
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

DATA = Path(__file__).resolve().parent / "data" / "intraday"

# US regular trading hours in UTC. Bars are stamped Z by Alpaca.
RTH_START, RTH_END = "13:30", "19:59"


def load(symbol: str, timeframe: str) -> list[dict]:
    path = DATA / f"{symbol.upper()}_{timeframe}.csv"
    if not path.exists():
        sys.exit(f"No data at {path}. Run: python fetch_intraday.py {symbol.upper()} "
                 f"--timeframe {timeframe}")
    bars = []
    with open(path, newline="") as f:
        for r in csv.DictReader(f):
            hhmm = r["t"][11:16]
            if not (RTH_START <= hhmm <= RTH_END):
                continue                      # regular hours only
            bars.append({"t": r["t"], "o": float(r["o"]), "h": float(r["h"]),
                         "l": float(r["l"]), "c": float(r["c"]),
                         "v": float(r["v"] or 0)})
    return bars


def run(bars, signals, target_bp, stop_bp, spread_bp, max_hold):
    """Walk the tape. Returns per-trade results in basis points, net of spread."""
    half = spread_bp / 2 / 10_000
    trades = []
    i, n = 0, len(bars)

    while i < n - 1:
        if not signals[i]:
            i += 1
            continue
        entry_idx = i + 1                                  # next bar, never this one
        session = bars[entry_idx]["t"][:10]
        entry = bars[entry_idx]["o"] * (1 + half)          # pay the offer
        target = entry * (1 + target_bp / 10_000)
        stop = entry * (1 - stop_bp / 10_000)

        exit_px, exit_idx, reason = None, None, None
        for j in range(entry_idx, min(entry_idx + max_hold, n)):
            b = bars[j]
            if b["t"][:10] != session:                     # never hold overnight
                exit_px, exit_idx, reason = bars[j - 1]["c"], j - 1, "session-end"
                break
            if b["l"] <= stop:                             # pessimistic: stop first
                exit_px, exit_idx, reason = stop, j, "stop"
                break
            if b["h"] >= target:
                exit_px, exit_idx, reason = target, j, "target"
                break
        if exit_px is None:
            k = min(entry_idx + max_hold, n) - 1
            exit_px, exit_idx, reason = bars[k]["c"], k, "timeout"

        exit_px *= (1 - half)                              # hit the bid on the way out
        trades.append({
            "t": bars[entry_idx]["t"], "entry": entry, "exit": exit_px,
            "bp": (exit_px / entry - 1) * 10_000, "reason": reason,
            "held": exit_idx - entry_idx + 1,
        })
        i = exit_idx + 1                                   # no overlapping positions
    return trades


def report(symbol, params, trades, bars, target_bp, stop_bp, spread_bp, max_hold):
    sessions = len({b["t"][:10] for b in bars})
    print(f"SCALP BACKTEST — {symbol}  {params}")
    print(f"{len(bars)} RTH bars over {sessions} sessions")
    print(f"target {target_bp}bp | stop {stop_bp}bp | spread {spread_bp}bp "
          f"round trip {spread_bp:.2f}bp | max hold {max_hold} bars")
    print()
    if not trades:
        print("  NO TRADES — the signal never fired. Loosen z_entry or check the data.")
        return
    bps = [t["bp"] for t in trades]
    wins = [b for b in bps if b > 0]
    total = sum(bps)
    mean = statistics.mean(bps)
    sd = statistics.pstdev(bps) if len(bps) > 1 else 0.0
    reasons = {}
    for t in trades:
        reasons[t["reason"]] = reasons.get(t["reason"], 0) + 1

    print(f"  trades            {len(trades)}   ({len(trades)/max(sessions,1):.1f} per session)")
    print(f"  win rate          {len(wins)/len(bps):.1%}")
    print(f"  mean edge/trade   {mean:+.3f} bp      <-- the number that matters")
    print(f"  median            {statistics.median(bps):+.3f} bp")
    print(f"  total             {total:+.1f} bp")
    print(f"  std dev           {sd:.2f} bp")
    if sd > 0:
        # per-trade Sharpe annualised on trade count, not calendar time
        per_year = len(trades) / max(sessions, 1) * 252
        print(f"  Sharpe (ann.)     {mean/sd*math.sqrt(per_year):.2f}")
    print(f"  avg hold          {statistics.mean([t['held'] for t in trades]):.1f} bars")
    print("  exits             " + "  ".join(f"{k}={v}" for k, v in sorted(reasons.items())))
    print()
    gross = mean + spread_bp
    print(f"  gross edge before spread  {gross:+.3f} bp")
    print(f"  spread paid per trade     {spread_bp:.3f} bp")
    print(f"  NET                       {mean:+.3f} bp")
    print()
    if mean <= 0:
        print("  VERDICT: NO EDGE. This loses money after costs. Do not trade it.")
    elif mean < spread_bp:
        print("  VERDICT: edge is smaller than the spread it pays. Too fragile to trade —")
        print("           any slippage beyond the quoted spread wipes it out.")
    else:
        print("  VERDICT: positive net edge in this sample. Sample is small; treat as a")
        print("           hypothesis to forward-test on paper, not a proven strategy.")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("strategy")
    ap.add_argument("symbol")
    ap.add_argument("--timeframe", default="1Min")
    ap.add_argument("--target-bp", type=float, default=10.0)
    ap.add_argument("--stop-bp", type=float, default=8.0)
    ap.add_argument("--spread-bp", type=float, default=0.26)
    ap.add_argument("--max-hold", type=int, default=20)
    ap.add_argument("--param", action="append", default=[])
    args = ap.parse_args()

    module = importlib.import_module(f"strategies.{args.strategy}")
    params = dict(module.PARAMS)
    for ov in args.param:
        k, _, v = ov.partition("=")
        if k not in params:
            sys.exit(f"Unknown param {k!r}; strategy params: {list(params)}")
        params[k] = type(params[k])(v)

    bars = load(args.symbol, args.timeframe)
    if len(bars) < 100:
        sys.exit(f"Only {len(bars)} RTH bars — not enough to say anything.")
    signals = module.generate_signals(bars, **params)
    trades = run(bars, signals, args.target_bp, args.stop_bp, args.spread_bp, args.max_hold)
    report(args.symbol.upper(), params, trades, bars,
           args.target_bp, args.stop_bp, args.spread_bp, args.max_hold)


if __name__ == "__main__":
    main()
