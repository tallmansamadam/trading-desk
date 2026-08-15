#!/usr/bin/env python
"""fetch_data.py — download daily OHLCV history for OFFLINE BACKTESTING.

This is a convenience source for testing the system when TWS/IB Gateway is not
running. It is NOT the production data path — live trading decisions should use
IBKR data via `python trade.py bars SYM --csv out.csv`, which is what you are
actually going to trade against.

Writes data/{SYMBOL}.csv with columns:
    date,open,high,low,close,adjclose,volume

`close` is the raw close; `adjclose` is adjusted for dividends and splits.
backtest.py prefers adjclose when present, so returns include dividends.

Usage:
  python fetch_data.py SPY QQQ AAPL --range 10y
  python fetch_data.py SPY --range 2y --interval 1d
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

# Windows consoles default to cp1252, which cannot encode characters the risk
# report uses (e.g. the U+2248 "almost equal" sign). That raises
# UnicodeEncodeError the moment output is piped or redirected — precisely how an
# agent or a log capture reads it. Force UTF-8 on the way out.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
DATA_DIR = Path(__file__).resolve().parent / "data"


def fetch(symbol: str, rng: str, interval: str, retries: int = 3) -> list[dict]:
    url = f"{CHART_URL.format(symbol=symbol.upper())}?range={rng}&interval={interval}"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})

    last_err: Exception | None = None
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                payload = json.load(resp)
            break
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            last_err = exc
            time.sleep(1.5 * (attempt + 1))
    else:
        raise SystemExit(f"{symbol}: download failed after {retries} tries ({last_err})")

    chart = payload.get("chart") or {}
    if chart.get("error"):
        raise SystemExit(f"{symbol}: provider error {chart['error']}")
    results = chart.get("result") or []
    if not results:
        raise SystemExit(f"{symbol}: no data returned (is the ticker valid?)")

    result = results[0]
    stamps = result.get("timestamp") or []
    quote = (result.get("indicators", {}).get("quote") or [{}])[0]
    adj_block = (result.get("indicators", {}).get("adjclose") or [{}])[0]
    adjclose = adj_block.get("adjclose") or [None] * len(stamps)

    rows = []
    for i, ts in enumerate(stamps):
        close = quote.get("close", [None] * len(stamps))[i]
        if close is None:  # halted/holiday rows come back null
            continue
        rows.append(
            {
                "date": datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d"),
                "open": quote.get("open", [None] * len(stamps))[i],
                "high": quote.get("high", [None] * len(stamps))[i],
                "low": quote.get("low", [None] * len(stamps))[i],
                "close": close,
                "adjclose": adjclose[i] if adjclose[i] is not None else close,
                "volume": quote.get("volume", [None] * len(stamps))[i],
            }
        )
    if not rows:
        raise SystemExit(f"{symbol}: provider returned rows but all closes were null")
    return rows


def write_csv(symbol: str, rows: list[dict]) -> Path:
    DATA_DIR.mkdir(exist_ok=True)
    path = DATA_DIR / f"{symbol.upper()}.csv"
    fields = ["date", "open", "high", "low", "close", "adjclose", "volume"]
    with open(path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow({k: ("" if row[k] is None else row[k]) for k in fields})
    return path


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("symbols", nargs="+")
    parser.add_argument("--range", dest="rng", default="10y",
                        help="1mo, 6mo, 1y, 2y, 5y, 10y, max (default 10y)")
    parser.add_argument("--interval", default="1d", help="1d, 1wk, 1mo (default 1d)")
    args = parser.parse_args()

    failures = []
    for symbol in args.symbols:
        try:
            rows = fetch(symbol, args.rng, args.interval)
        except SystemExit as exc:
            print(f"  {symbol.upper():<6} FAILED: {exc}", file=sys.stderr)
            failures.append(symbol)
            continue
        path = write_csv(symbol, rows)
        print(f"  {symbol.upper():<6} {len(rows):>5} bars  "
              f"{rows[0]['date']} -> {rows[-1]['date']}  {path.name}")
        time.sleep(0.4)  # be polite to the provider

    if failures:
        sys.exit(f"Failed: {', '.join(s.upper() for s in failures)}")


if __name__ == "__main__":
    main()
