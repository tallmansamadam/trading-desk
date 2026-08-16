#!/usr/bin/env python
"""reconcile.py — check what we believe against what the broker holds.

Every component here reads positions live from the broker, so there is no
second ledger to drift. What CAN drift is the record of intent: the journals say
an order was sent, and the broker is the only authority on whether it arrived,
filled, was rejected, or quietly expired. A system that never checks is a system
that finds out during an incident.

Three reconciliations, each answering a question you would otherwise guess at:

  ORDERS    every order the journals recorded sending — did it reach the broker?
            An order we think we placed and the broker has never heard of is the
            worst class of discrepancy, because the book is short a position
            nobody is watching for.

  BOOK      the allocator's target weights against the positions actually held.
            Drift is expected between rebalances; a name absent entirely, or one
            far past its cap, is not.

  LIMITS    current exposure against the configured caps. Both caps are ENTRY
            caps, so appreciation can carry a book past them without any order
            breaking a rule. Nothing else in the system reports that.

Read-only. It reports; it does not correct. Deciding what to do about a
discrepancy is a human's job, and a tool that silently "fixed" a reconciliation
break would destroy the evidence of what went wrong.

  python reconcile.py
  python reconcile.py --days 5 --json
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

from trading.brokers.alpaca import AlpacaBroker, AlpacaError
from trading.config import load_settings, trading_halted

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

LOG_DIR = Path(__file__).resolve().parent / "logs"
REPORT_DIR = Path(__file__).resolve().parent / "reports" / "reconciliation"

OK, WARN, BREAK = "OK", "WARN", "BREAK"


def mask(account: str) -> str:
    """Reports are committed to a public repository. The account identifier
    adds nothing to the audit value of a reconciliation, so it is masked by
    default; --full-account includes it for an internal record."""
    return f"{account[:2]}****{account[-3:]}" if len(account) > 6 else "****"


def journal_orders(days: int) -> list[dict]:
    """Orders the journals claim were SENT (shadow entries are not claims)."""
    cutoff = (datetime.now(UTC) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    out = []
    for path in sorted(LOG_DIR.glob("*.csv")):
        try:
            with open(path, newline="", encoding="utf-8") as f:
                for row in csv.DictReader(f):
                    if row.get("event") != "ORDER-SENT" or row.get("ts", "") < cutoff:
                        continue
                    detail = row.get("detail", "")
                    oid = ""
                    if "id=" in detail:
                        oid = detail.split("id=")[1].split()[0]
                    out.append({"ts": row["ts"], "symbol": row.get("symbol", ""),
                                "side": row.get("side", ""), "qty": row.get("qty", ""),
                                "id": oid, "source": path.name})
        except (OSError, csv.Error):
            continue
    return out


def broker_orders(broker: AlpacaBroker, days: int) -> dict[str, dict]:
    after = (datetime.now(UTC) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    rows = broker._trading("GET", "/v2/orders",
                           query={"status": "all", "limit": 500, "after": after})
    return {r["id"][:8]: r for r in rows}


def reconcile_orders(broker, days):
    recorded = journal_orders(days)
    actual = broker_orders(broker, days)
    findings, matched, orphans = [], 0, []

    for j in recorded:
        if not j["id"]:
            findings.append((WARN, f"journal row for {j['symbol']} has no broker id "
                                   f"({j['source']}) — cannot be verified"))
            continue
        if j["id"] in actual:
            matched += 1
        else:
            findings.append((BREAK, f"{j['symbol']} order {j['id']} was recorded as SENT "
                                    f"at {j['ts'][11:19]}Z but the broker has no such order"))

    known = {j["id"] for j in recorded if j["id"]}
    for oid, r in actual.items():
        if oid not in known:
            orphans.append(f"{r['symbol']} {r['side']} {r['qty']} ({r['status']}) id={oid}")

    return {"recorded": len(recorded), "at_broker": len(actual), "matched": matched,
            "findings": findings, "orphans": orphans}


def reconcile_book(broker, settings, symbols):
    held = {i.contract.symbol: i.marketValue for i in broker.portfolio()}
    gross_cap = getattr(settings, "max_gross_notional", None)
    equity = float(broker.account(refresh=True).get("equity") or 0)
    budget = min(equity, gross_cap or equity)
    per = min(budget / len(symbols), settings.max_position_notional) if symbols else 0

    rows, findings = [], []
    for sym in symbols:
        v = held.get(sym, 0.0)
        drift = (v - per) / per * 100 if per else 0
        rows.append((sym, v, per, drift))
        if v == 0:
            findings.append((WARN, f"{sym} is in the target universe but not held"))
        elif abs(drift) > 50:
            findings.append((WARN, f"{sym} is {drift:+.0f}% from its ${per:,.0f} target"))

    for sym, v in held.items():
        if sym not in symbols:
            findings.append((BREAK, f"{sym} is held (${v:,.0f}) but is not in the "
                                    f"target universe — nothing here put it there"))
    return {"rows": rows, "per_target": per, "findings": findings}


def reconcile_limits(broker, settings):
    items = broker.portfolio()
    gross = sum(abs(i.marketValue) for i in items)
    gross_cap = getattr(settings, "max_gross_notional", None)
    findings = []
    for i in items:
        v = abs(i.marketValue)
        if v > settings.max_position_notional:
            findings.append((WARN, f"{i.contract.symbol} at ${v:,.0f} is "
                                   f"{v/settings.max_position_notional:.1f}x its "
                                   f"${settings.max_position_notional:,.0f} cap — entry caps "
                                   f"do not trim, so only a human closes this"))
    if gross_cap and gross > gross_cap:
        findings.append((WARN, f"gross ${gross:,.0f} exceeds the ${gross_cap:,.0f} "
                               f"book cap by {gross/gross_cap - 1:.0%}"))
    return {"gross": gross, "cap": gross_cap, "findings": findings}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--days", type=int, default=3, help="how far back to reconcile orders")
    ap.add_argument("--symbols", nargs="+", default=None,
                    help="target universe (defaults to the service's)")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    ap.add_argument("--save", action="store_true",
                    help="also write a timestamped report to reports/reconciliation/")
    ap.add_argument("--full-account", action="store_true",
                    help="include the unmasked account number in the report")
    args = ap.parse_args()

    settings = load_settings()
    broker = AlpacaBroker(mode=settings.mode)
    account = broker.connect_and_verify()

    symbols = args.symbols
    if symbols is None:
        import desk_service
        symbols = desk_service.DEFAULT_UNIVERSE
    symbols = [s.upper() for s in symbols]

    orders = reconcile_orders(broker, args.days)
    book = reconcile_book(broker, settings, symbols)
    limits = reconcile_limits(broker, settings)
    findings = orders["findings"] + book["findings"] + limits["findings"]
    breaks = [f for lvl, f in findings if lvl == BREAK]
    warns = [f for lvl, f in findings if lvl == WARN]

    if args.json:
        print(json.dumps({"account": account, "halted": trading_halted(),
                          "orders": {k: v for k, v in orders.items() if k != "findings"},
                          "gross": limits["gross"], "breaks": breaks, "warnings": warns},
                         indent=2))
        sys.exit(1 if breaks else 0)

    shown_account = account if args.full_account else mask(account)
    out_lines: list[str] = []

    def emit(line: str = "") -> None:
        print(line)
        out_lines.append(line)

    emit("=" * 72)
    emit(f"  RECONCILIATION — account {shown_account}   last {args.days} day(s)")
    emit(f"  halted: {trading_halted()}")
    emit("=" * 72)

    emit(f"\nORDERS   journal says sent: {orders['recorded']}   "
          f"at broker: {orders['at_broker']}   matched: {orders['matched']}")
    for sym in orders["orphans"]:
        emit(f"  note   at the broker but not in any journal: {sym}")
    if not orders["recorded"]:
        emit("  (no ORDER-SENT rows in range — shadow runs do not count as claims)")

    emit(f"\nBOOK     target ${book['per_target']:,.0f} per name")
    for sym, v, per, drift in book["rows"]:
        flag = "" if v else "   <-- not held"
        emit(f"  {sym:<6} ${v:>10,.0f}  vs ${per:>9,.0f}   {drift:+7.0f}%{flag}")

    emit(f"\nLIMITS   gross ${limits['gross']:,.0f}"
          + (f" of ${limits['cap']:,.0f} cap "
             f"({limits['gross']/limits['cap']:.0%})" if limits["cap"] else ""))

    emit("\n" + "=" * 72)
    if breaks:
        emit(f"  {len(breaks)} BREAK(S) — these should not happen:")
        for f in breaks:
            emit(f"    ! {f}")
    if warns:
        emit(f"  {len(warns)} warning(s):")
        for f in warns:
            emit(f"    - {f}")
    if not breaks and not warns:
        emit("  CLEAN — the record and the broker agree.")
    emit("=" * 72)
    if args.save:
        REPORT_DIR.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(UTC).strftime("%Y-%m-%dT%H%M%SZ")
        verdict = "BREAK" if breaks else ("WARN" if warns else "CLEAN")
        path = REPORT_DIR / f"{stamp}-{verdict}.txt"
        header = [
            f"# reconciliation report — {verdict}",
            f"# generated {datetime.now(UTC).isoformat(timespec='seconds')}",
            f"# mode {settings.mode}   window {args.days}d",
            "# read-only: this records a discrepancy check, it does not correct one",
            "",
        ]
        path.write_text("\n".join(header + out_lines) + "\n", encoding="utf-8")
        print(f"\nsaved {path.relative_to(Path(__file__).resolve().parent)}")

    sys.exit(1 if breaks else 0)


if __name__ == "__main__":
    try:
        main()
    except AlpacaError as exc:
        sys.exit(str(exc))
