"""Market data helpers: snapshots and historical bars for US stocks."""

from __future__ import annotations

import math

from ib_async import IB, Stock


def stock(symbol: str) -> Stock:
    return Stock(symbol.upper(), "SMART", "USD")


def qualify(ib: IB, symbol: str) -> Stock:
    contract = stock(symbol)
    qualified = ib.qualifyContracts(contract)
    if not qualified:
        raise SystemExit(f"Could not qualify contract for symbol {symbol!r}")
    return qualified[0]


def snapshot(ib: IB, symbols: list[str]) -> list[dict]:
    """One-shot quote for each symbol (respects configured market data type)."""
    contracts = [qualify(ib, s) for s in symbols]
    tickers = ib.reqTickers(*contracts)
    rows = []
    for ticker in tickers:
        def _num(value):
            return None if value is None or (isinstance(value, float) and math.isnan(value)) else value

        rows.append(
            {
                "symbol": ticker.contract.symbol,
                "bid": _num(ticker.bid),
                "ask": _num(ticker.ask),
                "last": _num(ticker.last),
                "close": _num(ticker.close),
                "volume": _num(ticker.volume),
            }
        )
    return rows


def reference_price(ib: IB, symbol: str) -> float:
    """Best available price for risk sizing.

    Preference order: last trade, then mid, then the session close — all from
    the live snapshot. If the snapshot is empty (market closed, or delayed data
    that is not streaming) fall back to the most recent daily bar so the desk
    stays usable outside regular hours.

    The fallback is a STALE price and says so loudly: an overnight gap makes it
    an underestimate of true notional. Prefer an explicit --limit, which skips
    this path entirely because the limit price is what actually gets sized.
    """
    row = snapshot(ib, [symbol])[0]
    if row["last"]:
        return float(row["last"])
    if row["bid"] and row["ask"]:
        return (float(row["bid"]) + float(row["ask"])) / 2
    if row["close"]:
        return float(row["close"])

    try:
        bars = historical_bars(ib, symbol, duration="5 D", bar_size="1 day")
    except SystemExit:
        bars = []
    if bars:
        last_bar = bars[-1]
        print(
            f"WARNING: no live quote for {symbol.upper()} (market closed, or no "
            f"market data subscription). Sizing off the {last_bar.date} close "
            f"${float(last_bar.close):,.2f}. This price is STALE — pass --limit "
            f"for an accurate size."
        )
        return float(last_bar.close)

    raise SystemExit(
        f"No usable price for {symbol}; cannot size the order safely. "
        "No live quote and no historical bars available."
    )


def historical_bars(
    ib: IB,
    symbol: str,
    duration: str = "30 D",
    bar_size: str = "1 day",
    what_to_show: str = "TRADES",
):
    contract = qualify(ib, symbol)
    bars = ib.reqHistoricalData(
        contract,
        endDateTime="",
        durationStr=duration,
        barSizeSetting=bar_size,
        whatToShow=what_to_show,
        useRTH=True,
        formatDate=1,
    )
    if not bars:
        raise SystemExit(f"No historical bars returned for {symbol}")
    return bars
