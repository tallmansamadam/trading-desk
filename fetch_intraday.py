#!/usr/bin/env python
"""fetch_intraday.py — pull minute bars from Alpaca for intraday research.

Daily bars cannot tell you anything about scalping. This writes
data/intraday/{SYMBOL}_{TIMEFRAME}.csv so the scalp backtest has something
honest to run against.

  python fetch_intraday.py SPY QQQ --timeframe 1Min --limit 4000
"""

from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

from trading.brokers.alpaca import AlpacaBroker, AlpacaError
from trading.config import load_settings

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

OUT = Path(__file__).resolve().parent / "data" / "intraday"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("symbols", nargs="+")
    ap.add_argument("--timeframe", default="1Min")
    ap.add_argument("--limit", type=int, default=4000)
    args = ap.parse_args()

    settings = load_settings()
    broker = AlpacaBroker(mode=settings.mode)
    OUT.mkdir(parents=True, exist_ok=True)

    for sym in args.symbols:
        sym = sym.upper()
        try:
            bars = broker.historical_bars(sym, args.timeframe, args.limit)
        except AlpacaError as exc:
            print(f"  {sym:<6} FAILED: {exc}", file=sys.stderr)
            continue
        if not bars:
            print(f"  {sym:<6} no bars returned", file=sys.stderr)
            continue
        path = OUT / f"{sym}_{args.timeframe}.csv"
        with open(path, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["t", "o", "h", "l", "c", "v", "n", "vw"])
            for b in bars:
                w.writerow([b["t"], b["o"], b["h"], b["l"], b["c"],
                            b.get("v", 0), b.get("n", 0), b.get("vw", "")])
        sessions = len({b["t"][:10] for b in bars})
        print(f"  {sym:<6} {len(bars):>5} bars  {sessions} sessions  "
              f"{bars[0]['t'][:16]} -> {bars[-1]['t'][:16]}  {path.name}")


if __name__ == "__main__":
    try:
        main()
    except AlpacaError as exc:
        sys.exit(str(exc))
