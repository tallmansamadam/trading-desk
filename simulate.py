#!/usr/bin/env python
"""simulate.py — replay the whole desk over historical bars.

backtest.py answers "is the strategy any good?" in fractional-equity terms and
ignores the dollar risk limits entirely. This answers a different and more
practical question: **what would the desk actually have done** with a real
account size and the limits in .env?

It drives the REAL risk engine (trading/risk.py, unmodified) against a simulated
broker, so every order goes through the same checks that guard a live order:
order notional, position notional, daily loss, restricted symbols, open orders.
Rejections are counted and reported by reason.

Orders execute at the NEXT bar's open (a signal on today's close cannot be
filled at today's close), with costs modeled per side.

Usage:
  python simulate.py sma_cross SPY
  python simulate.py sma_cross SPY --account 100000 --cost-bps 5
  python simulate.py sma_cross QQQ --param fast=5 --param slow=20 --log 20
"""

from __future__ import annotations

import argparse
import csv
import importlib
import math
import sys
from collections import Counter
from pathlib import Path

from trading.config import load_settings
from trading.risk import check_order

# Windows consoles default to cp1252, which cannot encode characters the risk
# report uses (e.g. the U+2248 "almost equal" sign). That raises
# UnicodeEncodeError the moment output is piped or redirected — precisely how an
# agent or a log capture reads it. Force UTF-8 on the way out.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

DATA_DIR = Path(__file__).resolve().parent / "data"


# --- a broker that looks enough like ib_async.IB for the risk engine ---------

class SimContract:
    def __init__(self, symbol: str) -> None:
        self.symbol = symbol


class SimPosition:
    def __init__(self, symbol: str, position: float) -> None:
        self.contract = SimContract(symbol)
        self.position = position


class SimPnL:
    def __init__(self, daily: float) -> None:
        self.dailyPnL = daily
        self.unrealizedPnL = 0.0
        self.realizedPnL = 0.0


class SimPortfolioItem:
    def __init__(self, symbol: str, market_value: float) -> None:
        self.contract = SimContract(symbol)
        self.marketValue = market_value


class SimBroker:
    """Implements exactly the surface trading/risk.py touches."""

    def __init__(self) -> None:
        self.shares: dict[str, float] = {}
        self.marks: dict[str, float] = {}  # symbol -> latest close, for marking
        self.daily_pnl = 0.0

    def positions(self, account: str = "") -> list[SimPosition]:
        return [SimPosition(s, q) for s, q in self.shares.items() if q]

    def portfolio(self, account: str = "") -> list[SimPortfolioItem]:
        """Needed by the gross-exposure check, which marks the book to market."""
        return [
            SimPortfolioItem(s, q * self.marks.get(s, 0.0))
            for s, q in self.shares.items() if q
        ]

    def reqAllOpenOrders(self) -> list:
        return []  # the sim fills or rejects immediately; nothing rests

    def reqPnL(self, account: str, modelCode: str = "") -> SimPnL:
        return SimPnL(self.daily_pnl)

    def cancelPnL(self, account: str, modelCode: str = "") -> None:
        pass

    def sleep(self, _seconds: float) -> None:
        pass


# --- data -------------------------------------------------------------------

def load_bars(symbol: str) -> list[dict]:
    path = DATA_DIR / f"{symbol.upper()}.csv"
    if not path.exists():
        sys.exit(f"No data for {symbol.upper()}. Run: python fetch_data.py {symbol.upper()}")
    rows = []
    with open(path, newline="") as f:
        reader = csv.DictReader(f)
        price_field = "adjclose" if "adjclose" in (reader.fieldnames or []) else "close"
        for row in reader:
            if not row[price_field] or not row["open"]:
                continue
            # Scale the open by the same adjustment ratio as the close so the
            # bar stays internally consistent when using adjusted prices.
            raw_close = float(row["close"])
            adj_close = float(row[price_field])
            ratio = adj_close / raw_close if raw_close else 1.0
            rows.append({
                "date": row["date"],
                "open": float(row["open"]) * ratio,
                "close": adj_close,
            })
    return rows


# --- the replay -------------------------------------------------------------

def simulate(symbol, bars, signals, settings, account_size, cost_bps, log_rows):
    cost = cost_bps / 10_000
    broker = SimBroker()
    symbol = symbol.upper()

    cash = account_size
    equity_curve = [account_size]
    prev_equity = account_size

    rejections: Counter[str] = Counter()
    fills: list[dict] = []
    halt_days: list[str] = []
    journal: list[str] = []

    for i in range(len(bars) - 1):
        today, tomorrow = bars[i], bars[i + 1]
        fill_price = tomorrow["open"]
        held = broker.shares.get(symbol, 0.0)

        # Mark to today's close, then hand the risk engine today's P&L so the
        # real daily-loss check can fire exactly as it would live.
        broker.marks[symbol] = today["close"]
        equity = cash + held * today["close"]
        broker.daily_pnl = equity - prev_equity

        target_signal = signals[i]  # decided on today's close
        # Size to the position cap, not to conviction.
        want = math.floor(settings.max_position_notional / fill_price) if target_signal else 0

        if target_signal:
            # Scale in toward the cap, but tolerate drift. Recomputing the exact
            # share count every bar would churn a share back and forth forever as
            # the price wobbles, paying costs for nothing.
            delta = want - held if held < want * 0.95 else 0
        else:
            delta = -held  # exit fully

        if delta != 0:
            side = "BUY" if delta > 0 else "SELL"
            qty = abs(delta)
            # Respect the per-order cap by slicing; the desk would send the rest later.
            max_qty = math.floor(settings.max_order_notional / fill_price)
            qty = min(qty, max_qty) if max_qty > 0 else 0

            if qty > 0:
                verdict = check_order(
                    broker, "DUSIM", settings, symbol, side, qty, limit_price=fill_price
                )
                if verdict.approved:
                    signed = qty if side == "BUY" else -qty
                    notional = qty * fill_price
                    cash -= signed * fill_price + notional * cost
                    broker.shares[symbol] = held + signed
                    fills.append({
                        "date": tomorrow["date"], "side": side, "qty": qty,
                        "price": fill_price, "notional": notional,
                    })
                    if len(journal) < log_rows:
                        journal.append(
                            f"  {tomorrow['date']}  {side:<4} {qty:>4.0f} @ ${fill_price:>8.2f}"
                            f"  notional ${notional:>9,.2f}  pos {broker.shares[symbol]:>5.0f}"
                        )
                else:
                    for name, passed, detail in verdict.checks:
                        if not passed:
                            rejections[name] += 1
                            if name == "daily-loss" and tomorrow["date"] not in halt_days:
                                halt_days.append(tomorrow["date"])
                            if len(journal) < log_rows:
                                journal.append(
                                    f"  {tomorrow['date']}  REJECTED {side} {qty:.0f} "
                                    f"— {name}: {detail}"
                                )

        held = broker.shares.get(symbol, 0.0)
        prev_equity = equity
        equity_curve.append(cash + held * tomorrow["close"])

    return {
        "equity_curve": equity_curve,
        "fills": fills,
        "rejections": rejections,
        "halt_days": halt_days,
        "journal": journal,
        "final_shares": broker.shares.get(symbol, 0.0),
    }


def stats(equity_curve, account_size):
    final = equity_curve[-1]
    peak, max_dd = equity_curve[0], 0.0
    for e in equity_curve:
        peak = max(peak, e)
        max_dd = max(max_dd, 1 - e / peak)
    daily = [equity_curve[i] / equity_curve[i - 1] - 1 for i in range(1, len(equity_curve))]
    mean = sum(daily) / len(daily)
    var = sum((d - mean) ** 2 for d in daily) / max(len(daily) - 1, 1)
    sharpe = (mean / math.sqrt(var) * math.sqrt(252)) if var > 0 else 0.0
    return {
        "final": final,
        "pnl": final - account_size,
        "return": final / account_size - 1,
        "max_dd": max_dd,
        "sharpe": sharpe,
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("strategy")
    parser.add_argument("symbol")
    parser.add_argument("--account", type=float, default=100_000.0)
    parser.add_argument("--cost-bps", type=float, default=5.0)
    parser.add_argument("--param", action="append", default=[])
    parser.add_argument("--log", type=int, default=12, help="journal lines to print")
    args = parser.parse_args()

    settings = load_settings()
    module = importlib.import_module(f"strategies.{args.strategy}")
    params = dict(module.PARAMS)
    for override in args.param:
        key, _, value = override.partition("=")
        if key not in params:
            sys.exit(f"Unknown param {key!r}; strategy params: {list(params)}")
        params[key] = type(params[key])(value)

    bars = load_bars(args.symbol)
    closes = [b["close"] for b in bars]
    signals = module.generate_signals(closes, **params)

    result = simulate(
        args.symbol, bars, signals, settings, args.account, args.cost_bps, args.log
    )
    s = stats(result["equity_curve"], args.account)

    sym = args.symbol.upper()
    print(f"DESK REPLAY — {module.NAME} {params} on {sym}")
    print(f"{bars[0]['date']} -> {bars[-1]['date']}  ({len(bars)} bars, {args.cost_bps} bps/side)")
    print()
    print("Limits in force (from .env):")
    print(f"  max order notional    ${settings.max_order_notional:>12,.2f}")
    print(f"  max position notional ${settings.max_position_notional:>12,.2f}   per symbol")
    gross_cap = getattr(settings, "max_gross_notional", None)
    if gross_cap is not None:
        print(f"  max gross notional    ${gross_cap:>12,.2f}   book-wide "
              f"(one symbol here, so it never binds)")
    print(f"  max daily loss        ${settings.max_daily_loss:>12,.2f}")
    print(f"  restricted symbols    {', '.join(settings.restricted_symbols) or '(none)'}")
    print()
    print("Account:")
    print(f"  starting equity       ${args.account:>12,.2f}")
    print(f"  ending equity         ${s['final']:>12,.2f}")
    print(f"  P&L                   ${s['pnl']:>12,.2f}   ({s['return']:+.2%})")
    print(f"  max drawdown          {s['max_dd']:>12.2%}")
    print(f"  Sharpe (ann.)         {s['sharpe']:>12.2f}")
    print()
    buy_hold = closes[-1] / closes[0] - 1
    capped = settings.max_position_notional / args.account
    print(f"  buy & hold (unconstrained) {buy_hold:+.2%} on the full account")
    print(f"  position cap allows at most {capped:.1%} of the account in this name")
    print()
    print(f"Orders filled: {len(result['fills'])}")
    if result["rejections"]:
        print("Orders REJECTED by the risk engine:")
        for name, count in result["rejections"].most_common():
            print(f"  {name:<22} {count:>5}")
    else:
        print("Orders rejected: none")
    if result["halt_days"]:
        print(f"Days the daily-loss limit blocked trading: {len(result['halt_days'])}")
        print(f"  first few: {', '.join(result['halt_days'][:5])}")
    print()
    if result["journal"]:
        print(f"Journal (first {len(result['journal'])} events):")
        for line in result["journal"]:
            print(line)


if __name__ == "__main__":
    main()
