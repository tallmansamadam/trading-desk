#!/usr/bin/env python
"""simulate_portfolio.py — replay the desk across MANY symbols on ONE account.

simulate.py gives each symbol its own account, which hides the thing that
actually matters at the portfolio level: MAX_POSITION_NOTIONAL is a *per-symbol*
cap, so N symbols can each hold the cap simultaneously and nothing in the risk
engine objects. This replays every symbol against a single shared cash balance
and a single shared position book, driving the real trading/risk.py checks.

What to watch:
  - peak gross exposure vs account equity (the uncapped dimension)
  - daily-loss rejections, which finally bite once the whole book is at risk
  - minimum cash, i.e. whether the desk quietly went on margin

Usage:
  python simulate_portfolio.py sma_cross SPY QQQ AAPL MSFT TLT GLD IWM
  python simulate_portfolio.py sma_cross SPY QQQ --account 100000
"""

from __future__ import annotations

import argparse
import importlib
import math
import sys
from collections import Counter

from simulate import SimBroker, load_bars, stats
from trading.config import load_settings
from trading.risk import check_order

# Windows consoles default to cp1252, which cannot encode characters the risk
# report uses (e.g. the U+2248 "almost equal" sign). That raises
# UnicodeEncodeError the moment output is piped or redirected — precisely how an
# agent or a log capture reads it. Force UTF-8 on the way out.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


def align(symbols: list[str]) -> tuple[list[str], dict[str, dict[str, dict]]]:
    """Return the dates common to every symbol, plus date-indexed bars."""
    per_symbol: dict[str, dict[str, dict]] = {}
    for sym in symbols:
        per_symbol[sym] = {b["date"]: b for b in load_bars(sym)}

    common = set.intersection(*(set(d.keys()) for d in per_symbol.values()))
    dates = sorted(common)
    if len(dates) < 50:
        sys.exit(f"Only {len(dates)} overlapping dates across {symbols}; not enough to replay.")
    return dates, per_symbol


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("strategy")
    parser.add_argument("symbols", nargs="+")
    parser.add_argument("--account", type=float, default=100_000.0)
    parser.add_argument("--cost-bps", type=float, default=5.0)
    parser.add_argument("--param", action="append", default=[])
    args = parser.parse_args()

    settings = load_settings()
    module = importlib.import_module(f"strategies.{args.strategy}")
    params = dict(module.PARAMS)
    for override in args.param:
        key, _, value = override.partition("=")
        if key not in params:
            sys.exit(f"Unknown param {key!r}; strategy params: {list(params)}")
        params[key] = type(params[key])(value)

    symbols = [s.upper() for s in args.symbols]
    dates, bars = align(symbols)

    # Signals per symbol, computed on that symbol's own close series.
    signals: dict[str, dict[str, int]] = {}
    for sym in symbols:
        series = [bars[sym][d] for d in dates]
        closes = [b["close"] for b in series]
        sig = module.generate_signals(closes, **params)
        signals[sym] = dict(zip(dates, sig, strict=True))

    cost = args.cost_bps / 10_000
    broker = SimBroker()
    cash = args.account
    prev_equity = args.account
    equity_curve = [args.account]

    rejections: Counter[str] = Counter()
    rejected_symbols: Counter[str] = Counter()
    halt_days: set[str] = set()
    fills = 0
    min_cash = cash
    peak_gross = 0.0
    peak_position: dict[str, float] = {}
    peak_gross_date = dates[0]
    peak_gross_pct = 0.0
    days_over_half = 0

    for i in range(len(dates) - 1):
        today, tomorrow = dates[i], dates[i + 1]

        # Mark the whole book to today's close, then hand the risk engine the
        # book-level daily P&L — the same number a live reqPnL would return.
        for sym in symbols:
            broker.marks[sym] = bars[sym][today]["close"]

        gross = sum(
            abs(q) * bars[sym][today]["close"] for sym, q in broker.shares.items() if q
        )
        equity = cash + sum(
            q * bars[sym][today]["close"] for sym, q in broker.shares.items() if q
        )
        broker.daily_pnl = equity - prev_equity

        for sym, q in broker.shares.items():
            if q:
                value = abs(q) * bars[sym][today]["close"]
                if value > peak_position.get(sym, 0.0):
                    peak_position[sym] = value

        if gross > peak_gross:
            peak_gross, peak_gross_date = gross, today
            peak_gross_pct = gross / equity if equity else 0.0
        if equity and gross > 0.5 * equity:
            days_over_half += 1

        for sym in symbols:  # fixed order; ties in cash go to the earlier symbol
            fill_price = bars[sym][tomorrow]["open"]
            held = broker.shares.get(sym, 0.0)
            want = (
                math.floor(settings.max_position_notional / fill_price)
                if signals[sym][today] else 0
            )
            if signals[sym][today]:
                delta = want - held if held < want * 0.95 else 0
            else:
                delta = -held
            if delta == 0:
                continue

            side = "BUY" if delta > 0 else "SELL"
            qty = abs(delta)
            max_qty = math.floor(settings.max_order_notional / fill_price)
            qty = min(qty, max_qty) if max_qty > 0 else 0
            if qty <= 0:
                continue

            verdict = check_order(
                broker, "DUSIM", settings, sym, side, qty, limit_price=fill_price
            )
            if verdict.approved:
                signed = qty if side == "BUY" else -qty
                cash -= signed * fill_price + qty * fill_price * cost
                broker.shares[sym] = held + signed
                fills += 1
                min_cash = min(min_cash, cash)
            else:
                for name, passed, _detail in verdict.checks:
                    if not passed:
                        rejections[name] += 1
                        rejected_symbols[sym] += 1
                        if name == "daily-loss":
                            halt_days.add(tomorrow)

        prev_equity = equity
        equity_curve.append(
            cash + sum(
                q * bars[sym][tomorrow]["close"] for sym, q in broker.shares.items() if q
            )
        )

    s = stats(equity_curve, args.account)

    print(f"PORTFOLIO REPLAY — {module.NAME} {params}")
    print(f"{', '.join(symbols)}  ({len(symbols)} symbols on ONE account)")
    print(f"{dates[0]} -> {dates[-1]}  ({len(dates)} bars, {args.cost_bps} bps/side)")
    print()
    print("Limits in force (from .env):")
    print(f"  max order notional    ${settings.max_order_notional:>12,.2f}   per order")
    print(f"  max position notional ${settings.max_position_notional:>12,.2f}   PER SYMBOL")
    print(f"  max daily loss        ${settings.max_daily_loss:>12,.2f}   book-wide")
    gross_cap = getattr(settings, "max_gross_notional", None)
    if gross_cap is None:
        print(f"  max gross notional    {'(no cap — NOT enforced)':>25}")
    else:
        print(f"  max gross notional    ${gross_cap:>12,.2f}   book-wide, enforced")
    print(f"  per-symbol caps sum to ${settings.max_position_notional * len(symbols):>11,.2f}"
          f"   ({len(symbols)} x ${settings.max_position_notional:,.0f})")
    print()
    print("Account:")
    print(f"  starting equity       ${args.account:>12,.2f}")
    print(f"  ending equity         ${s['final']:>12,.2f}")
    print(f"  P&L                   ${s['pnl']:>12,.2f}   ({s['return']:+.2%})")
    print(f"  max drawdown          {s['max_dd']:>12.2%}")
    print(f"  Sharpe (ann.)         {s['sharpe']:>12.2f}")
    print()
    print("Exposure:")
    print(f"  peak gross            ${peak_gross:>12,.2f}   on {peak_gross_date}"
          f"  ({peak_gross_pct:.1%} of equity)")
    print(f"  days gross > 50% eq   {days_over_half:>12,}   of {len(dates) - 1}")
    print(f"  minimum cash          ${min_cash:>12,.2f}"
          f"{'   <-- NEGATIVE: bought on margin' if min_cash < 0 else ''}")
    print()
    print(f"Peak value reached by each position (cap at ENTRY is "
          f"${settings.max_position_notional:,.0f}):")
    for sym, value in sorted(peak_position.items(), key=lambda kv: -kv[1]):
        over = value / settings.max_position_notional
        flag = "  <-- grew past the cap" if over > 1.05 else ""
        print(f"  {sym:<6} ${value:>11,.2f}   {over:>5.2f}x cap{flag}")
    print()
    print(f"Orders filled: {fills}")
    if rejections:
        print("Orders REJECTED by the risk engine:")
        for name, count in rejections.most_common():
            print(f"  {name:<22} {count:>6}")
        print("  by symbol: " + ", ".join(
            f"{s}={c}" for s, c in rejected_symbols.most_common()
        ))
    else:
        print("Orders rejected: none")
    if halt_days:
        print(f"Distinct days the daily-loss limit blocked new orders: {len(halt_days)}")
        print(f"  earliest: {', '.join(sorted(halt_days)[:5])}")


if __name__ == "__main__":
    main()
