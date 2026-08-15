"""Account, position and P&L reporting."""

from __future__ import annotations

from ib_async import IB

_SUMMARY_TAGS = (
    "NetLiquidation",
    "TotalCashValue",
    "BuyingPower",
    "GrossPositionValue",
    "AvailableFunds",
    "MaintMarginReq",
)


def account_status(ib: IB, account: str, mode: str) -> str:
    rows = [v for v in ib.accountSummary(account) if v.tag in _SUMMARY_TAGS]
    lines = [f"Account: {account}  Mode: {mode.upper()}"]
    for row in sorted(rows, key=lambda r: _SUMMARY_TAGS.index(r.tag)):
        lines.append(f"  {row.tag:<20} {float(row.value):>15,.2f} {row.currency}")
    return "\n".join(lines)


def positions_table(ib: IB, account: str) -> str:
    items = ib.portfolio(account)
    if not items:
        return "No positions."
    lines = [
        f"{'Symbol':<8}{'Qty':>10}{'AvgCost':>12}{'MktPrice':>12}"
        f"{'MktValue':>14}{'UnrealPnL':>12}{'RealPnL':>12}"
    ]
    for item in items:
        lines.append(
            f"{item.contract.symbol:<8}{item.position:>10,.0f}"
            f"{item.averageCost:>12,.2f}{item.marketPrice:>12,.2f}"
            f"{item.marketValue:>14,.2f}{item.unrealizedPNL:>12,.2f}"
            f"{item.realizedPNL:>12,.2f}"
        )
    total_upnl = sum(i.unrealizedPNL for i in items)
    total_rpnl = sum(i.realizedPNL for i in items)
    lines.append(f"{'TOTAL':<8}{'':>34}{'':>14}{total_upnl:>12,.2f}{total_rpnl:>12,.2f}")
    return "\n".join(lines)


def open_orders_table(ib: IB) -> str:
    trades = ib.reqAllOpenOrders()
    if not trades:
        return "No open orders."
    lines = [
        f"{'Id':<10}{'Symbol':<8}{'Side':<6}{'Qty':>8}  {'Type':<6}"
        f"{'LmtPx':>10}  {'Filled':>8}  {'Status':<12}"
    ]
    for t in trades:
        lines.append(
            f"{t.order.orderId:<10}{t.contract.symbol:<8}{t.order.action:<6}"
            f"{t.order.totalQuantity:>8,.0f}  {t.order.orderType:<6}"
            f"{(t.order.lmtPrice or 0):>10,.2f}  {t.orderStatus.filled:>8,.0f}  "
            f"{t.orderStatus.status:<12}"
        )
    return "\n".join(lines)


def pnl_summary(ib: IB, account: str) -> str:
    pnl = ib.reqPnL(account)
    ib.sleep(1.5)
    ib.cancelPnL(account)

    def _fmt(value):
        if value is None or value != value:  # NaN guard
            return "n/a"
        return f"${value:,.2f}"

    return (
        f"Daily P&L:      {_fmt(pnl.dailyPnL)}\n"
        f"Unrealized P&L: {_fmt(pnl.unrealizedPnL)}\n"
        f"Realized P&L:   {_fmt(pnl.realizedPnL)}"
    )
