#!/usr/bin/env python
"""backtest.py — evaluate a strategy from strategies/ against IBKR history.

Usage:
  python backtest.py sma_cross SPY --duration "2 Y" --size "1 day"
  python backtest.py sma_cross SPY --csv bars.csv          # offline, from CSV
  python backtest.py sma_cross SPY --param fast=5 --param slow=20

Signals are next-bar executed (signal on bar i -> position held over bar i+1)
to avoid lookahead bias. Costs modeled as a flat per-side rate (default 5 bps).
"""

from __future__ import annotations

import argparse
import csv
import importlib
import math
import sys

# Windows consoles default to cp1252, which cannot encode characters the risk
# report uses (e.g. the U+2248 "almost equal" sign). That raises
# UnicodeEncodeError the moment output is piped or redirected — precisely how an
# agent or a log capture reads it. Force UTF-8 on the way out.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


def load_closes_csv(path: str) -> tuple[list[str], list[float]]:
    """Load a bar CSV. Prefers 'adjclose' (dividend/split adjusted) when the
    column is present, so returns are total-return rather than price-only.
    Falls back to 'close' for CSVs written by `trade.py bars`."""
    dates, closes = [], []
    with open(path, newline="") as f:
        reader = csv.DictReader(f)
        field = "adjclose" if "adjclose" in (reader.fieldnames or []) else "close"
        for row in reader:
            if not row[field]:
                continue
            dates.append(row["date"])
            closes.append(float(row[field]))
    if not closes:
        sys.exit(f"No usable price data in {path}")
    return dates, closes


def load_closes_ibkr(symbol: str, duration: str, size: str) -> tuple[list[str], list[float]]:
    from trading.connection import ibkr_session
    from trading.market_data import historical_bars

    with ibkr_session() as (ib, _account, _settings):
        bars = historical_bars(ib, symbol, duration, size)
    return [str(b.date) for b in bars], [float(b.close) for b in bars]


def run_backtest(closes: list[float], signals: list[int], cost_bps: float) -> dict:
    if len(signals) != len(closes):
        sys.exit("Strategy bug: signals length != closes length")

    cost = cost_bps / 10_000
    equity = [1.0]
    position = 0
    trades = 0
    trade_returns: list[float] = []
    entry_equity = None

    for i in range(1, len(closes)):
        # The signal computed on bar i-1's close is the one we can act on going
        # into bar i, so the position is updated BEFORE bar i's return is
        # applied. Updating it after would silently add a second bar of lag.
        target = signals[i - 1]
        cost_mult = 1.0
        if target != position:
            cost_mult = 1 - cost
            fill_equity = equity[-1] * cost_mult  # equity at the moment of the fill
            if target == 1:
                entry_equity = fill_equity
            elif entry_equity:
                trade_returns.append(fill_equity / entry_equity - 1)
                entry_equity = None
            trades += 1
            position = target

        bar_return = closes[i] / closes[i - 1] - 1
        step = (1 + (bar_return if position == 1 else 0)) * cost_mult
        equity.append(equity[-1] * step)

    total_return = equity[-1] - 1
    peak, max_dd = equity[0], 0.0
    for e in equity:
        peak = max(peak, e)
        max_dd = max(max_dd, 1 - e / peak)

    daily = [equity[i] / equity[i - 1] - 1 for i in range(1, len(equity))]
    mean = sum(daily) / len(daily)
    var = sum((d - mean) ** 2 for d in daily) / max(len(daily) - 1, 1)
    sharpe = (mean / math.sqrt(var) * math.sqrt(252)) if var > 0 else 0.0
    wins = sum(1 for r in trade_returns if r > 0)

    return {
        "bars": len(closes),
        "total_return": total_return,
        "buy_hold_return": closes[-1] / closes[0] - 1,
        "max_drawdown": max_dd,
        "sharpe": sharpe,
        "round_trips": len(trade_returns),
        "win_rate": wins / len(trade_returns) if trade_returns else float("nan"),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("strategy", help="module name in strategies/ (e.g. sma_cross)")
    parser.add_argument("symbol")
    parser.add_argument("--duration", default="1 Y")
    parser.add_argument("--size", default="1 day")
    parser.add_argument("--csv", help="load bars from CSV instead of IBKR")
    parser.add_argument("--cost-bps", type=float, default=5.0)
    parser.add_argument("--param", action="append", default=[], help="override strategy param, e.g. fast=5")
    args = parser.parse_args()

    module = importlib.import_module(f"strategies.{args.strategy}")
    params = dict(module.PARAMS)
    for override in args.param:
        key, _, value = override.partition("=")
        if key not in params:
            sys.exit(f"Unknown param {key!r}; strategy params: {list(params)}")
        params[key] = type(params[key])(value)

    if args.csv:
        dates, closes = load_closes_csv(args.csv)
    else:
        dates, closes = load_closes_ibkr(args.symbol, args.duration, args.size)

    signals = module.generate_signals(closes, **params)
    stats = run_backtest(closes, signals, args.cost_bps)

    print(f"Strategy {module.NAME} {params} on {args.symbol.upper()} "
          f"({dates[0]} -> {dates[-1]}, {stats['bars']} bars, {args.cost_bps} bps/side)")
    print(f"  Total return:   {stats['total_return']:+.2%}")
    print(f"  Buy & hold:     {stats['buy_hold_return']:+.2%}")
    print(f"  Max drawdown:   {stats['max_drawdown']:.2%}")
    print(f"  Sharpe (ann.):  {stats['sharpe']:.2f}")
    if stats["round_trips"]:
        print(f"  Round trips:    {stats['round_trips']}  Win rate: {stats['win_rate']:.0%}")
        if stats["round_trips"] < 20:
            print("  NOTE: fewer than 20 round trips — treat these stats as inconclusive.")
    else:
        print("  Round trips:    0 (strategy never entered)")


if __name__ == "__main__":
    main()
