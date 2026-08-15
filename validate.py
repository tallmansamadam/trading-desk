#!/usr/bin/env python
"""validate.py — the gate a strategy must clear before it earns capital.

A single backtest that looks good means almost nothing. Seven symbols times five
parameter sets is thirty-five chances to find a fluke, and this repo has already
produced several: sma_cross 5/20 beat a risk-matched benchmark on SPY by 105
percentage points and then improved Sharpe on only 3 of 7 symbols. That is what
selection looks like from the inside, and it is why blessing a strategy by
eyeballing one chart is not a process.

Five gates, each of which has actually killed something here:

  1 GENERALISATION  does the effect survive a universe, or is it one symbol?
  2 OUT-OF-SAMPLE   fit on the first half, verify on the second.
  3 RISK-MATCHED    does it beat simply holding less, at equal drawdown?
                    Scaling exposure cannot change Sharpe, so a timing rule
                    that fails here is adding complexity for nothing.
  4 COST STRESS     does it survive twice the assumed spread?
  5 PARAMETER       is it a plateau or a lone spike? A spike is a fit.

Three outcomes per gate, not two. INSUFFICIENT is not a pass: too few symbols
or too few trades means the test could not run, and unproven is not proven.

  python validate.py sma_cross
  python validate.py sma_cross --param fast=5 --param slow=20
  python validate.py sma_cross --universe SPY QQQ IWM --cost-bps 5
"""

from __future__ import annotations

import argparse
import csv
import importlib
import math
import statistics
import sys
from pathlib import Path

from backtest import run_backtest

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

DATA = Path(__file__).resolve().parent / "data"
DEFAULT_UNIVERSE = ["SPY", "QQQ", "AAPL", "MSFT", "TLT", "GLD", "IWM"]

PASS, FAIL, INSUF = "PASS", "FAIL", "INSUFFICIENT"


def load(symbol: str) -> list[float]:
    path = DATA / f"{symbol.upper()}.csv"
    if not path.exists():
        return []
    with open(path, newline="") as f:
        reader = csv.DictReader(f)
        field = "adjclose" if "adjclose" in (reader.fieldnames or []) else "close"
        return [float(r[field]) for r in reader if r.get(field)]


def curve_stats(equity: list[float]) -> tuple[float, float, float]:
    """(total return, max drawdown, annualised Sharpe)."""
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


def buy_hold(closes: list[float], fraction: float = 1.0) -> list[float]:
    """Equity from holding `fraction` of the instrument, remainder in cash."""
    eq = [1.0]
    for i in range(1, len(closes)):
        eq.append(eq[-1] * (1 + fraction * (closes[i] / closes[i - 1] - 1)))
    return eq


def evaluate(module, closes: list[float], params: dict, cost_bps: float) -> dict:
    sig = module.generate_signals(closes, **params)
    return run_backtest(closes, sig, cost_bps)


def t_stat(values: list[float]) -> tuple[float, float, float]:
    """(mean, standard error, t). Zero se yields t=0 rather than a divide error."""
    n = len(values)
    if n < 2:
        return (values[0] if values else 0.0), 0.0, 0.0
    m = statistics.mean(values)
    se = statistics.pstdev(values) / math.sqrt(n)
    return m, se, (m / se if se else 0.0)


# --- the gates ---------------------------------------------------------------

def gate_generalisation(module, universe, params, cost_bps):
    deltas, rows = [], []
    for sym in universe:
        d = load(sym)
        if len(d) < 250:
            continue
        res = evaluate(module, d, params, cost_bps)
        _, _, bh_sharpe = curve_stats(buy_hold(d))
        delta = res["sharpe"] - bh_sharpe
        deltas.append(delta)
        rows.append((sym, bh_sharpe, res["sharpe"], delta, res["round_trips"]))
    if len(deltas) < 5:
        return INSUF, f"only {len(deltas)} symbols with usable data — need 5+", rows
    m, se, t = t_stat(deltas)
    wins = sum(1 for x in deltas if x > 0)
    detail = (f"mean Sharpe delta {m:+.3f}, se {se:.3f}, t {t:.2f}, "
              f"improved on {wins}/{len(deltas)} symbols")
    if t >= 2.0:
        return PASS, detail, rows
    if wins <= len(deltas) / 2:
        return FAIL, detail + " — helps on half or fewer; this is symbol selection", rows
    return FAIL, detail + " — not distinguishable from noise (needs t>=2)", rows


def gate_out_of_sample(module, universe, params, cost_bps):
    is_d, oos_d = [], []
    for sym in universe:
        d = load(sym)
        if len(d) < 500:
            continue
        mid = len(d) // 2
        for half, bucket in ((d[:mid], is_d), (d[mid:], oos_d)):
            res = evaluate(module, half, params, cost_bps)
            _, _, bh = curve_stats(buy_hold(half))
            bucket.append(res["sharpe"] - bh)
    if len(oos_d) < 5:
        return INSUF, f"only {len(oos_d)} symbols long enough to split", None
    mi, _, _ = t_stat(is_d)
    mo, seo, to = t_stat(oos_d)
    detail = f"in-sample delta {mi:+.3f} -> out-of-sample {mo:+.3f} (t {to:.2f})"
    if mo <= 0:
        return FAIL, detail + " — the edge does not survive the split", None
    if mi > 0 and mo < mi * 0.5:
        return FAIL, detail + " — over half the edge evaporates out-of-sample", None
    return PASS, detail, None


def gate_risk_matched(module, universe, params, cost_bps):
    """Beat holding less of the same thing, at the same drawdown."""
    beats, gaps = 0, []
    tested = 0
    for sym in universe:
        d = load(sym)
        if len(d) < 250:
            continue
        res = evaluate(module, d, params, cost_bps)
        target_dd = res["max_drawdown"]
        if target_dd <= 0:
            continue
        # find the exposure whose drawdown matches the strategy's
        best_f, best_gap = None, 1e9
        for step in range(5, 105, 5):
            f = step / 100
            _, dd, _ = curve_stats(buy_hold(d, f))
            if abs(dd - target_dd) < best_gap:
                best_f, best_gap = f, abs(dd - target_dd)
        if best_f is None:
            continue
        r_bh, _, _ = curve_stats(buy_hold(d, best_f))
        tested += 1
        gap = res["total_return"] - r_bh
        gaps.append(gap)
        if gap > 0:
            beats += 1
    if tested < 5:
        return INSUF, f"only {tested} symbols evaluated", None
    m, _, _ = t_stat(gaps)
    detail = (f"beats the risk-matched hold on {beats}/{tested} symbols, "
              f"mean excess {m:+.1%}")
    if beats > tested / 2 and m > 0:
        return PASS, detail, None
    return FAIL, detail + " — position sizing alone does as well or better", None


def gate_cost_stress(module, universe, params, cost_bps):
    base, stressed = [], []
    for sym in universe:
        d = load(sym)
        if len(d) < 250:
            continue
        b = evaluate(module, d, params, cost_bps)
        s = evaluate(module, d, params, cost_bps * 2)
        _, _, bh = curve_stats(buy_hold(d))
        base.append(b["sharpe"] - bh)
        stressed.append(s["sharpe"] - bh)
    if len(base) < 5:
        return INSUF, f"only {len(base)} symbols evaluated", None
    mb, _, _ = t_stat(base)
    ms, _, _ = t_stat(stressed)
    detail = f"delta at {cost_bps}bp {mb:+.3f} -> at {cost_bps*2}bp {ms:+.3f}"
    if ms <= 0:
        return FAIL, detail + " — doubling costs erases it; too fragile to trade"
    return PASS, detail, None


def gate_parameter_plateau(module, universe, params, cost_bps):
    """A good parameter sits on a plateau. A lone spike is a curve fit."""
    numeric = {k: v for k, v in params.items() if isinstance(v, (int, float))}
    if not numeric:
        return INSUF, "strategy exposes no numeric parameters to perturb", None

    def universe_score(p):
        vals = []
        for sym in universe:
            d = load(sym)
            if len(d) < 250:
                continue
            res = evaluate(module, d, p, cost_bps)
            _, _, bh = curve_stats(buy_hold(d))
            vals.append(res["sharpe"] - bh)
        return statistics.mean(vals) if vals else 0.0

    centre = universe_score(params)
    neighbours = []
    for key, val in numeric.items():
        for mult in (0.75, 1.5):
            cand = dict(params)
            nv = type(val)(max(2, round(val * mult)))
            if nv == val:
                continue
            cand[key] = nv
            try:
                neighbours.append(universe_score(cand))
            except Exception:
                continue
    if len(neighbours) < 2:
        return INSUF, "could not build enough neighbouring parameter sets", None
    med = statistics.median(neighbours)
    detail = (f"centre {centre:+.3f}, neighbours median {med:+.3f} "
              f"over {len(neighbours)} perturbations")
    if centre <= 0:
        return FAIL, detail + " — the chosen point is not even positive"
    if med < centre * 0.4:
        return FAIL, detail + " — a lone spike; neighbours collapse, so this is a fit"
    return PASS, detail, None


GATES = [
    ("GENERALISATION", "holds across a universe, not one lucky symbol", gate_generalisation),
    ("OUT-OF-SAMPLE", "survives fitting early and testing late", gate_out_of_sample),
    ("RISK-MATCHED", "beats simply holding less, at equal drawdown", gate_risk_matched),
    ("COST STRESS", "survives twice the assumed spread", gate_cost_stress),
    ("PARAMETER", "sits on a plateau rather than a spike", gate_parameter_plateau),
]


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("strategy")
    ap.add_argument("--universe", nargs="+", default=DEFAULT_UNIVERSE)
    ap.add_argument("--cost-bps", type=float, default=5.0)
    ap.add_argument("--param", action="append", default=[])
    args = ap.parse_args()

    module = importlib.import_module(f"strategies.{args.strategy}")
    params = dict(module.PARAMS)
    for ov in args.param:
        k, _, v = ov.partition("=")
        if k not in params:
            sys.exit(f"Unknown param {k!r}; strategy params: {list(params)}")
        params[k] = type(params[k])(v)

    universe = [s.upper() for s in args.universe]
    print("=" * 74)
    print(f"  VALIDATING  {module.NAME}  {params}")
    print(f"  universe {' '.join(universe)}   cost {args.cost_bps} bp/side")
    print("=" * 74)

    results, detail_rows = [], None
    for name, why, fn in GATES:
        try:
            out = fn(module, universe, params, args.cost_bps)
            verdict, detail = out[0], out[1]
            if name == "GENERALISATION" and len(out) > 2:
                detail_rows = out[2]
        except Exception as exc:
            verdict, detail = INSUF, f"gate errored: {type(exc).__name__}: {exc}"
        results.append((name, verdict, detail))
        mark = {PASS: "PASS", FAIL: "FAIL", INSUF: "????"}[verdict]
        print(f"\n[{mark}] {name}  — {why}")
        print(f"       {detail}")

    if detail_rows:
        print("\n  per-symbol Sharpe (buy & hold -> strategy):")
        for sym, bh, st, delta, rt in detail_rows:
            flag = "" if delta > 0 else "   <-- worse"
            print(f"       {sym:<6}{bh:>7.2f} ->{st:>7.2f}   {delta:+.2f}"
                  f"   {rt} round trips{flag}")

    print("\n" + "=" * 74)
    failed = [n for n, v, _ in results if v == FAIL]
    unknown = [n for n, v, _ in results if v == INSUF]
    if failed:
        print(f"  REJECTED — failed {len(failed)} gate(s): {', '.join(failed)}")
        print("  Do not commit capital to this. The first failure above says why.")
    elif unknown:
        print(f"  NOT PROVEN — {len(unknown)} gate(s) could not run: {', '.join(unknown)}")
        print("  Unproven is not the same as proven. Get more data and re-run.")
    else:
        print("  PASSED ALL GATES.")
        print("  That earns a paper forward-test, not capital. Backtests are")
        print("  hypotheses; only out-of-sample live behaviour is evidence.")
    print("=" * 74)
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
