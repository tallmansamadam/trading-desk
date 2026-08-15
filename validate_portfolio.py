#!/usr/bin/env python
"""validate_portfolio.py — the gate an ALLOCATION rule must clear.

validate.py judges single-asset timing rules, and six of them have now failed
it. The one thing in this repo that measurably improved risk-adjusted return was
not a timing rule at all: a seven-symbol book ran at Sharpe 1.05 where SPY alone
ran at 0.77, using the same mediocre signal. That effect lives in portfolio
construction, which validate.py structurally cannot see.

This tests the other class. The benchmark is naive equal weight, deliberately:
DeMiguel, Garlappi and Uppal (2009) found no optimising rule reliably beat 1/N
once estimation error was counted. Beating 1/N is the bar, and it is a high one.

Five gates, adapted to what actually goes wrong in allocation:

  1 BEATS 1/N        against equal weight, bootstrapped over sub-universes so
                     the comparison carries a t-statistic instead of one number
  2 OUT-OF-SAMPLE    fit early, verify late
  3 NOT ONE ASSET    jackknife: drop each holding in turn. If any single name
                     carries the result, it is a stock pick wearing a portfolio
  4 COST STRESS      rebalancing has turnover; survive twice the assumed cost
  5 CADENCE          works across rebalance frequencies, not one magic number

  python validate_portfolio.py risk_parity
  python validate_portfolio.py equal_weight --universe SPY QQQ TLT GLD
"""

from __future__ import annotations

import argparse
import csv
import importlib
import math
import random
import statistics
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

DATA = Path(__file__).resolve().parent / "data"
PASS, FAIL, INSUF = "PASS", "FAIL", "INSUFFICIENT"

DEFAULT_UNIVERSE = ["SPY", "QQQ", "IWM", "AAPL", "MSFT", "TLT", "GLD",
                    "XLK", "XLF", "XLV", "XLY", "XLP", "XLE", "XLI", "XLB",
                    "XLU", "EFA", "EEM", "VNQ", "HYG", "LQD", "DBC", "IEF", "SLV"]


# --- data --------------------------------------------------------------------

def load_aligned(symbols: list[str]) -> tuple[list[str], dict[str, list[float]]]:
    """Closes for every symbol on the dates ALL of them share."""
    per: dict[str, dict[str, float]] = {}
    for sym in symbols:
        path = DATA / f"{sym.upper()}.csv"
        if not path.exists():
            continue
        with open(path, newline="") as f:
            reader = csv.DictReader(f)
            field = "adjclose" if "adjclose" in (reader.fieldnames or []) else "close"
            per[sym.upper()] = {r["date"]: float(r[field]) for r in reader if r.get(field)}
    if not per:
        return [], {}
    common = sorted(set.intersection(*(set(d) for d in per.values())))
    return common, {s: [per[s][d] for d in common] for s in per}


# --- engine ------------------------------------------------------------------

def run_portfolio(closes: dict[str, list[float]], weights: list[dict[str, float]],
                  cost_bps: float) -> dict:
    """Weights decided on bar i are held over bar i+1 — one bar of lag, matching
    backtest.py. Cost is charged on the L1 change in weights."""
    n = len(next(iter(closes.values())))
    cost = cost_bps / 10_000
    held: dict[str, float] = {}
    equity = [1.0]
    turnover_total = 0.0
    rebalances = 0

    for i in range(1, n):
        target = weights[i - 1] if i - 1 < len(weights) else {}
        cost_mult = 1.0
        if target:
            names = set(held) | set(target)
            churn = sum(abs(target.get(s, 0.0) - held.get(s, 0.0)) for s in names)
            if churn > 1e-9:
                cost_mult = 1 - cost * churn
                turnover_total += churn
                rebalances += 1
                held = dict(target)

        rets = {}
        for s in held:
            prev = closes[s][i - 1]
            rets[s] = (closes[s][i] / prev - 1) if prev else 0.0
        step = sum(held[s] * rets[s] for s in held)
        equity.append(equity[-1] * (1 + step) * cost_mult)

        # Let the weights DRIFT with prices. Without this the book is silently
        # rebalanced every bar, turnover reads near zero, and the rebalance
        # cadence has no effect — which would make a rebalancing study
        # measure nothing at all.
        denom = 1 + step
        if abs(denom) > 1e-12:
            held = {s: held[s] * (1 + rets[s]) / denom for s in held}

    years = n / 252
    return {"equity": equity,
            "turnover_per_year": turnover_total / years if years else 0.0,
            "rebalances": rebalances}


def curve_stats(equity: list[float]) -> tuple[float, float, float]:
    peak, dd = equity[0], 0.0
    for e in equity:
        peak = max(peak, e)
        dd = max(dd, 1 - e / peak)
    rets = [equity[i] / equity[i - 1] - 1 for i in range(1, len(equity))]
    if not rets:
        return 0.0, 0.0, 0.0
    m = sum(rets) / len(rets)
    v = statistics.pstdev(rets)
    return equity[-1] - 1, dd, (m / v * math.sqrt(252) if v else 0.0)


def sharpe_of(module, closes, params, cost_bps) -> float:
    n = len(next(iter(closes.values())))
    w = module.generate_weights(closes, n, **params)
    return curve_stats(run_portfolio(closes, w, cost_bps)["equity"])[2]


def benchmark_sharpe(closes, params, cost_bps) -> float:
    import portfolios.equal_weight as ew
    n = len(next(iter(closes.values())))
    reb = params.get("rebalance_days", 21)
    w = ew.generate_weights(closes, n, rebalance_days=reb)
    return curve_stats(run_portfolio(closes, w, cost_bps)["equity"])[2]


def t_stat(vals: list[float]) -> tuple[float, float, float]:
    if len(vals) < 2:
        return (vals[0] if vals else 0.0), 0.0, 0.0
    m = statistics.mean(vals)
    se = statistics.pstdev(vals) / math.sqrt(len(vals))
    return m, se, (m / se if se else 0.0)


# --- gates -------------------------------------------------------------------

def gate_beats_equal_weight(module, closes, params, cost_bps, subs=40, size=10):
    """Bootstrap sub-universes so a single portfolio comparison gains a t-stat."""
    symbols = sorted(closes)
    if len(symbols) < size + 2:
        size = max(4, len(symbols) // 2)
    if len(symbols) < 6:
        return INSUF, f"universe of {len(symbols)} is too small to bootstrap", None
    rng = random.Random(20260815)
    deltas = []
    for _ in range(subs):
        pick = rng.sample(symbols, size)
        sub = {s: closes[s] for s in pick}
        deltas.append(sharpe_of(module, sub, params, cost_bps)
                      - benchmark_sharpe(sub, params, cost_bps))
    m, se, t = t_stat(deltas)
    wins = sum(1 for d in deltas if d > 0)
    detail = (f"mean Sharpe vs 1/N {m:+.3f}, se {se:.3f}, t {t:.2f}, "
              f"better on {wins}/{subs} random sub-universes of {size}")
    if t >= 2.0 and m > 0:
        return PASS, detail, deltas
    if m <= 0:
        return FAIL, detail + " — does not beat naive equal weight", deltas
    return FAIL, detail + " — positive but not distinguishable from noise", deltas


def gate_out_of_sample(module, closes, params, cost_bps):
    n = len(next(iter(closes.values())))
    if n < 800:
        return INSUF, f"only {n} bars — too short to split", None
    mid = n // 2
    halves = []
    for lo, hi in ((0, mid), (mid, n)):
        seg = {s: v[lo:hi] for s, v in closes.items()}
        halves.append(sharpe_of(module, seg, params, cost_bps)
                      - benchmark_sharpe(seg, params, cost_bps))
    a, b = halves
    detail = f"first half {a:+.3f} vs 1/N, second half {b:+.3f}"
    if b <= 0:
        return FAIL, detail + " — the advantage does not survive the split", None
    if a > 0 and b < a * 0.5:
        return FAIL, detail + " — over half the advantage evaporates", None
    return PASS, detail, None


def gate_not_one_asset(module, closes, params, cost_bps):
    """Jackknife. If dropping any single holding kills it, it was a stock pick."""
    symbols = sorted(closes)
    if len(symbols) < 6:
        return INSUF, f"universe of {len(symbols)} too small to jackknife", None
    full = sharpe_of(module, closes, params, cost_bps) - \
        benchmark_sharpe(closes, params, cost_bps)
    worst_sym, worst = None, full
    for s in symbols:
        sub = {k: v for k, v in closes.items() if k != s}
        d = sharpe_of(module, sub, params, cost_bps) - \
            benchmark_sharpe(sub, params, cost_bps)
        if d < worst:
            worst, worst_sym = d, s
    detail = (f"full universe {full:+.3f}; worst drop-one is {worst:+.3f} "
              f"(without {worst_sym})")
    if full <= 0:
        return FAIL, detail + " — no advantage to attribute", None
    if worst <= 0:
        return FAIL, detail + f" — removing {worst_sym} erases it entirely", None
    if worst < full * 0.4:
        return FAIL, detail + f" — {worst_sym} carries most of the result", None
    return PASS, detail, None


def gate_cost_stress(module, closes, params, cost_bps):
    base = sharpe_of(module, closes, params, cost_bps) - \
        benchmark_sharpe(closes, params, cost_bps)
    hard = sharpe_of(module, closes, params, cost_bps * 2) - \
        benchmark_sharpe(closes, params, cost_bps * 2)
    n = len(next(iter(closes.values())))
    w = module.generate_weights(closes, n, **params)
    turn = run_portfolio(closes, w, cost_bps)["turnover_per_year"]
    detail = (f"advantage {base:+.3f} at {cost_bps}bp -> {hard:+.3f} at "
              f"{cost_bps*2}bp; turnover {turn:.1f}x/yr")
    if hard <= 0:
        return FAIL, detail + " — doubling costs erases it", None
    return PASS, detail, None


def gate_cadence(module, closes, params, cost_bps):
    """A rule that only works at one rebalance frequency is fitted to it."""
    if "rebalance_days" not in params:
        return INSUF, "rule exposes no rebalance cadence to vary", None
    scores = {}
    for days in (5, 21, 63):
        p = dict(params, rebalance_days=days)
        scores[days] = sharpe_of(module, closes, p, cost_bps) - \
            benchmark_sharpe(closes, p, cost_bps)
    detail = "  ".join(f"{d}d {v:+.3f}" for d, v in scores.items())
    positive = sum(1 for v in scores.values() if v > 0)
    if positive == len(scores):
        return PASS, detail, None
    if positive == 0:
        return FAIL, detail + " — negative at every cadence", None
    return FAIL, detail + f" — works at only {positive}/{len(scores)} cadences", None


GATES = [
    ("BEATS 1/N", "clears naive equal weight, with a t-stat", gate_beats_equal_weight),
    ("OUT-OF-SAMPLE", "survives fitting early and testing late", gate_out_of_sample),
    ("NOT ONE ASSET", "no single holding carries the result", gate_not_one_asset),
    ("COST STRESS", "survives twice the rebalancing cost", gate_cost_stress),
    ("CADENCE", "works across rebalance frequencies", gate_cadence),
]


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("rule")
    ap.add_argument("--universe", nargs="+", default=DEFAULT_UNIVERSE)
    ap.add_argument("--cost-bps", type=float, default=5.0)
    ap.add_argument("--param", action="append", default=[])
    args = ap.parse_args()

    module = importlib.import_module(f"portfolios.{args.rule}")
    params = dict(module.PARAMS)
    for ov in args.param:
        k, _, v = ov.partition("=")
        if k not in params:
            sys.exit(f"Unknown param {k!r}; rule params: {list(params)}")
        params[k] = type(params[k])(v)

    dates, closes = load_aligned([s.upper() for s in args.universe])
    if len(closes) < 4:
        sys.exit(f"Only {len(closes)} symbols had data — need at least 4.")

    print("=" * 76)
    print(f"  VALIDATING ALLOCATION RULE  {module.NAME}  {params}")
    print(f"  {len(closes)} symbols, {len(dates)} shared bars "
          f"({dates[0]} -> {dates[-1]}), cost {args.cost_bps} bp")
    print("=" * 76)

    results = []
    for name, why, fn in GATES:
        try:
            verdict, detail = fn(module, closes, params, args.cost_bps)[:2]
        except Exception as exc:
            verdict, detail = INSUF, f"gate errored: {type(exc).__name__}: {exc}"
        results.append((name, verdict))
        mark = {PASS: "PASS", FAIL: "FAIL", INSUF: "????"}[verdict]
        print(f"\n[{mark}] {name}  — {why}")
        print(f"       {detail}")

    # headline comparison for context
    full = sharpe_of(module, closes, params, args.cost_bps)
    bench = benchmark_sharpe(closes, params, args.cost_bps)
    n = len(dates)
    eq_rule = run_portfolio(closes, module.generate_weights(closes, n, **params),
                            args.cost_bps)["equity"]
    r_rule, dd_rule, _ = curve_stats(eq_rule)
    print(f"\n  full universe:  {module.NAME} Sharpe {full:.2f} vs 1/N {bench:.2f}"
          f"   return {r_rule:+.1%}  maxDD {dd_rule:.1%}")

    print("\n" + "=" * 76)
    failed = [n for n, v in results if v == FAIL]
    unknown = [n for n, v in results if v == INSUF]
    if failed:
        print(f"  REJECTED — failed {len(failed)} gate(s): {', '.join(failed)}")
    elif unknown:
        print(f"  NOT PROVEN — {len(unknown)} gate(s) could not run: {', '.join(unknown)}")
        print("  Unproven is not proven.")
    else:
        print("  PASSED ALL GATES.")
        print("  Earns a paper forward-test, not capital.")
    print("=" * 76)
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
