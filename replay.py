#!/usr/bin/env python
"""replay.py — run the real desk over history, blind, at speed.

`simulate.py` replays history through the risk engine. This replays it through
the ENTIRE production stack: desk_service.Service drives run_desk.Allocator
drives trading/risk.py drives a broker — the same objects the live desk uses,
unmodified. Only the broker is swapped, for one backed by historical bars and a
simulated clock.

That band of code is where every behavioural bug in this system has lived: the
60-second spin on a settled book, duplicate orders while one was in flight, an
exit path that raised NameError. All three were found by watching production.
All three would have surfaced in one replayed session.

    python replay.py --start 2020-01-02 --end 2020-06-30     # the COVID crash
    python replay.py --days 250 --pathology                  # a year, with checks

Blind by construction: the Allocator only ever sees bars up to the current
index, and the fill model never looks forward. It is NOT a fill guarantee —
fills are all-or-nothing at the touch, with no partials, no queue position and
no slippage beyond the limit.

Live state is never touched. The service state file and journals are redirected
to a scratch directory for the duration.
"""

from __future__ import annotations

import argparse
import csv
import sys
import tempfile
import types
from collections import Counter
from pathlib import Path

from trading.brokers.replay import ReplayBroker

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"

DEFAULT_UNIVERSE = ["SPY", "QQQ", "IWM", "EFA", "EEM", "TLT",
                    "IEF", "LQD", "HYG", "GLD", "DBC", "VNQ"]


def load(symbols: list[str], start: str | None, end: str | None):
    """Bars on the dates every symbol shares, so the book is never guessing."""
    per: dict[str, dict[str, dict]] = {}
    for sym in symbols:
        path = DATA / f"{sym}.csv"
        if not path.exists():
            continue
        rows = {}
        with open(path, newline="") as f:
            reader = csv.DictReader(f)
            adj = "adjclose" in (reader.fieldnames or [])
            for r in reader:
                d = r["date"]
                if (start and d < start) or (end and d > end):
                    continue
                close = float(r["adjclose"]) if adj and r["adjclose"] else float(r["close"])
                raw = float(r["close"]) or close
                k = close / raw                      # scale OHL onto the adjusted close
                rows[d] = {"o": float(r["open"]) * k, "h": float(r["high"]) * k,
                           "l": float(r["low"]) * k, "c": close,
                           "v": float(r["volume"] or 0)}
        if rows:
            per[sym] = rows
    if not per:
        sys.exit("No data. Run: python fetch_data.py " + " ".join(symbols))
    dates = sorted(set.intersection(*(set(v) for v in per.values())))
    return dates, {s: [per[s][d] for d in dates] for s in per}


def build_service(broker, symbols, scratch: Path, args):
    """Construct the REAL Service, pointed at scratch state and armed."""
    import desk_service
    import run_desk
    desk_service.LOG_DIR = scratch
    desk_service.STATE_FILE = scratch / "service_state.json"
    run_desk.LOG_DIR = scratch
    run_desk.Allocator.STATE = scratch / "allocator_state.json"

    from trading.config import load_settings
    settings = load_settings()
    if settings.is_live:
        sys.exit("REFUSED: replay refuses to run with TRADING_MODE=live, even "
                 "though it cannot reach a broker. The habit matters.")

    svc_args = types.SimpleNamespace(
        symbols=symbols, invested=args.invested, rebalance_days=args.rebalance_days,
        drift_pct=args.drift_pct, min_trade=args.min_trade,
        check_seconds=60.0, open_delay=args.open_delay, idle_backoff=args.idle_backoff,
        port=0, host="127.0.0.1", no_browser=True)
    svc = desk_service.Service(broker, settings, svc_args)
    svc.state.data["armed"] = True                   # replay exists to watch it trade
    return svc, settings


def pathology(broker, svc, ticks: int, sessions: int) -> list[tuple[str, str]]:
    """Behavioural checks the unit tests cannot make, because they are about
    what the desk does over TIME rather than in one call."""
    out = []
    per_symbol = Counter(f.symbol for f in broker.fills)
    orders_placed = broker._oid

    rebalances = sum(1 for e in svc.state.data["events"] if e["kind"] == "REBALANCE")
    if rebalances > sessions * 1.5:
        out.append(("SPIN", f"{rebalances} rebalance passes over {sessions} sessions — "
                            f"the desk is re-deciding far more often than the cadence "
                            f"implies"))

    churn = [(s, n) for s, n in per_symbol.items() if n > sessions / 10 + 6]
    for s, n in sorted(churn, key=lambda x: -x[1])[:5]:
        out.append(("CHURN", f"{s} filled {n} times in {sessions} sessions — a monthly "
                             f"rebalance should not touch one name that often"))

    if orders_placed and len(broker.fills) / orders_placed < 0.5:
        out.append(("UNFILLED", f"only {len(broker.fills)} of {orders_placed} orders "
                                f"filled — limits are priced away from the tape"))

    gross = sum(abs(q) * broker.price(s) for s, q in broker.shares.items())
    cap = getattr(svc.s, "max_gross_notional", None)
    if cap and gross > cap * 1.05:
        out.append(("EXPOSURE", f"gross ${gross:,.0f} finished {gross/cap - 1:.0%} over "
                                f"the ${cap:,.0f} cap"))
    if broker.cash < 0:
        out.append(("MARGIN", f"cash went negative (${broker.cash:,.0f}) — the desk "
                              f"borrowed without being told it could"))
    if broker.rejects:
        out.append(("REJECT", f"{len(broker.rejects)} order(s) the broker refused"))
    if svc.state.data["events"] and any(
            e["kind"] in ("ERROR", "BROKER-ERROR") for e in svc.state.data["events"]):
        errs = [e for e in svc.state.data["events"] if e["kind"] in ("ERROR", "BROKER-ERROR")]
        out.append(("CRASH", f"{len(errs)} unhandled error(s), e.g. {errs[0]['detail'][:70]}"))
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--symbols", nargs="+", default=DEFAULT_UNIVERSE)
    ap.add_argument("--start", default=None, help="first date, YYYY-MM-DD")
    ap.add_argument("--end", default=None)
    ap.add_argument("--days", type=int, default=None, help="use the last N sessions")
    ap.add_argument("--equity", type=float, default=100_000.0)
    ap.add_argument("--cost-bps", type=float, default=2.0)
    ap.add_argument("--ticks-per-day", type=int, default=3,
                    help="scheduler wake-ups per session")
    ap.add_argument("--invested", type=float, default=100.0)
    ap.add_argument("--rebalance-days", type=int, default=21)
    ap.add_argument("--drift-pct", type=float, default=25.0)
    ap.add_argument("--min-trade", type=float, default=200.0)
    ap.add_argument("--open-delay", type=float, default=5.0)
    ap.add_argument("--idle-backoff", type=float, default=15.0)
    ap.add_argument("--pathology", action="store_true", help="run behavioural checks")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    symbols = [s.upper() for s in args.symbols]
    dates, bars = load(symbols, args.start, args.end)
    if args.days:
        dates, bars = dates[-args.days:], {s: v[-args.days:] for s, v in bars.items()}
    if len(dates) < 30:
        sys.exit(f"Only {len(dates)} shared sessions — too few to replay.")

    scratch = Path(tempfile.mkdtemp(prefix="replay_"))
    broker = ReplayBroker(bars, dates, args.equity, args.cost_bps)
    svc, _settings = build_service(broker, symbols, scratch, args)

    print("=" * 74)
    print(f"  REPLAY — {len(symbols)} symbols, {len(dates)} sessions "
          f"({dates[0]} -> {dates[-1]})")
    print("  running the real Service + Allocator + risk engine; broker is history")
    print(f"  ${args.equity:,.0f} start, {args.cost_bps} bp/side, "
          f"{args.ticks_per_day} scheduler wake-ups per session")
    print("=" * 74)

    step = ReplayBroker.SESSION_MINUTES / max(args.ticks_per_day, 1)
    sessions = 0
    while True:
        for k in range(args.ticks_per_day):
            broker.set_minute(args.open_delay + 1 + k * step)
            try:
                svc.tick()
            except Exception as exc:                 # a crash IS the finding
                svc.state.log("ERROR", f"{type(exc).__name__}: {exc}"[:120])
        sessions += 1
        if not broker.advance():
            break

    eq = broker.equity()
    ret = eq / args.equity - 1
    peak, dd = args.equity, 0.0
    for _, v in broker.equity_curve:
        peak = max(peak, v)
        dd = max(dd, 1 - v / peak)

    print(f"\nRESULT   equity ${eq:,.2f}   return {ret:+.2%}   max drawdown {dd:.1%}")
    print(f"         {len(broker.fills)} fills from {broker._oid} orders, "
          f"{len(broker.resting)} still resting")
    print(f"         {broker.api_calls:,} broker calls "
          f"({broker.api_calls/max(sessions,1):.0f} per session)")
    held = {s: q * broker.price(s) for s, q in broker.shares.items() if q}
    print(f"         final book: {len(held)} names, "
          f"${sum(held.values()):,.0f} gross")
    if args.verbose:
        for s, v in sorted(held.items()):
            print(f"           {s:<6} ${v:>10,.0f}")

    if args.pathology:
        print("\n" + "=" * 74)
        findings = pathology(broker, svc, args.ticks_per_day, sessions)
        if findings:
            print(f"  {len(findings)} PATHOLOGY FINDING(S):")
            for kind, detail in findings:
                print(f"    [{kind}] {detail}")
        else:
            print("  NO PATHOLOGIES — behaviour over time looks sane.")
        print("=" * 74)
        sys.exit(1 if findings else 0)


if __name__ == "__main__":
    main()
