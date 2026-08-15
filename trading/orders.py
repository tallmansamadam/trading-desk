"""Order placement and position management. All paths run the risk engine."""

from __future__ import annotations

from ib_async import IB, LimitOrder, MarketOrder

from .config import Settings, trading_halted
from .market_data import qualify
from .risk import check_order


def place_order(
    ib: IB,
    account: str,
    settings: Settings,
    symbol: str,
    side: str,
    quantity: float,
    limit_price: float | None = None,
    tif: str = "DAY",
    confirm_live: bool = False,
) -> str:
    side = side.upper()

    if settings.is_live and not confirm_live:
        raise SystemExit(
            "REFUSED: TRADING_MODE=live but --confirm-live was not passed. "
            "Live orders require BOTH the LIVE_TRADING_ACK env var (set by a "
            "human) and the --confirm-live flag on the exact order command."
        )

    risk = check_order(ib, account, settings, symbol, side, quantity, limit_price)
    print(risk.report())
    if not risk.approved:
        raise SystemExit("REFUSED: risk checks failed — order not sent.")

    contract = qualify(ib, symbol)
    if limit_price:
        order = LimitOrder(side, quantity, limit_price, tif=tif, account=account)
    else:
        order = MarketOrder(side, quantity, tif=tif, account=account)

    trade = ib.placeOrder(contract, order)

    # While the status is PendingSubmit, TWS has not acknowledged the order and
    # filled/remaining are still zero-defaults. Printing them at that point reads
    # as "nothing outstanding" when in fact nothing has happened yet, so wait for
    # the status to settle before reporting.
    for _ in range(12):
        ib.sleep(0.5)
        if trade.orderStatus.status not in ("", "PendingSubmit"):
            break
    status = trade.orderStatus

    if status.status in ("", "PendingSubmit"):
        fill_detail = "TWS has not acknowledged yet — fill state unknown"
    else:
        fill_detail = (
            f"filled={status.filled:,.0f} remaining={status.remaining:,.0f} "
            f"avgFillPrice={status.avgFillPrice or 0:.2f}"
        )

    return (
        f"Order sent [{settings.mode.upper()}] {side} {quantity} {symbol.upper()} "
        f"{f'@ {limit_price:.2f} LMT' if limit_price else 'MKT'} tif={tif} | "
        f"id={trade.order.orderId} status={status.status or 'PendingSubmit'} | "
        f"{fill_detail}"
    )


def cancel_all(ib: IB) -> str:
    open_trades = ib.reqAllOpenOrders()
    if not open_trades:
        return "No open orders to cancel."
    ib.reqGlobalCancel()
    ib.sleep(2)
    return f"Global cancel sent for {len(open_trades)} open order(s)."


def flatten(
    ib: IB,
    account: str,
    settings: Settings,
    symbol: str | None = None,
    confirm_live: bool = False,
) -> str:
    """Close positions with market orders. The kill switch does NOT block
    flattening — reducing risk must always be possible — but live mode still
    requires confirmation."""
    if settings.is_live and not (confirm_live and settings.live_ack_present):
        raise SystemExit(
            "REFUSED: flattening a LIVE account requires LIVE_TRADING_ACK and "
            "--confirm-live."
        )

    positions = [
        p for p in ib.positions(account)
        if p.position != 0 and (symbol is None or p.contract.symbol == symbol.upper())
    ]
    if not positions:
        return "Nothing to flatten."

    lines = []
    for pos in positions:
        side = "SELL" if pos.position > 0 else "BUY"
        qty = abs(pos.position)
        contract = pos.contract
        contract.exchange = contract.exchange or "SMART"
        trade = ib.placeOrder(contract, MarketOrder(side, qty, account=account))
        ib.sleep(1)
        lines.append(
            f"{side} {qty} {contract.symbol} -> {trade.orderStatus.status}"
        )
    if trading_halted():
        lines.append("(note: HALT file present — flatten allowed, new entries blocked)")
    return "\n".join(lines)
