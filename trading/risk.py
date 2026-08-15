"""Pre-trade risk engine. Every order MUST pass check_order() before placement.

The checks here are hard limits enforced in code — agents cannot talk their
way around them. Loosening a limit requires a human editing .env.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ib_async import IB

from .config import Settings, trading_halted
from .market_data import reference_price


@dataclass
class RiskResult:
    approved: bool
    checks: list[tuple[str, bool, str]] = field(default_factory=list)

    def add(self, name: str, passed: bool, detail: str) -> None:
        self.checks.append((name, passed, detail))
        if not passed:
            self.approved = False

    def report(self) -> str:
        lines = []
        for name, passed, detail in self.checks:
            mark = "PASS" if passed else "FAIL"
            lines.append(f"[{mark}] {name}: {detail}")
        verdict = "APPROVED" if self.approved else "REJECTED"
        lines.append(f"=> {verdict}")
        return "\n".join(lines)


def _current_position(ib: IB, account: str, symbol: str) -> float:
    for pos in ib.positions(account):
        if pos.contract.symbol == symbol.upper():
            return float(pos.position)
    return 0.0


def _gross_exposure(ib: IB, account: str) -> float:
    """Absolute market value summed across every open position.

    Uses portfolio() rather than positions() because PortfolioItem already
    carries marketValue — no extra quote requests, and it reflects the current
    mark rather than an entry price.
    """
    return sum(abs(float(item.marketValue)) for item in ib.portfolio(account))


def _daily_pnl(ib: IB, account: str) -> float | None:
    """Realized + unrealized P&L for today, or None if unavailable."""
    pnl = ib.reqPnL(account)
    ib.sleep(1.5)  # give the subscription a moment to populate
    ib.cancelPnL(account)
    if pnl.dailyPnL is None or pnl.dailyPnL != pnl.dailyPnL:  # NaN guard
        return None
    return float(pnl.dailyPnL)


def check_order(
    ib: IB,
    account: str,
    settings: Settings,
    symbol: str,
    side: str,
    quantity: float,
    limit_price: float | None = None,
) -> RiskResult:
    symbol = symbol.upper()
    side = side.upper()
    result = RiskResult(approved=True)

    result.add(
        "kill-switch",
        not trading_halted(),
        "HALT file present — trading is halted" if trading_halted() else "no HALT file",
    )

    result.add(
        "restricted-list",
        symbol not in settings.restricted_symbols,
        f"{symbol} restricted: {symbol in settings.restricted_symbols}",
    )

    result.add(
        "side/quantity",
        side in ("BUY", "SELL") and quantity > 0,
        f"side={side} qty={quantity}",
    )

    price = limit_price if limit_price else reference_price(ib, symbol)
    notional = abs(quantity) * price
    result.add(
        "order-notional",
        notional <= settings.max_order_notional,
        f"${notional:,.2f} vs limit ${settings.max_order_notional:,.2f} (price ref ${price:,.2f})",
    )

    signed_qty = quantity if side == "BUY" else -quantity
    current_qty = _current_position(ib, account, symbol)
    resulting = current_qty + signed_qty
    resulting_notional = abs(resulting) * price
    result.add(
        "position-notional",
        resulting_notional <= settings.max_position_notional,
        f"resulting position {resulting:+.0f} sh ≈ ${resulting_notional:,.2f} "
        f"vs limit ${settings.max_position_notional:,.2f}",
    )

    # Gross exposure across the whole book. max_position_notional is PER SYMBOL,
    # so without this N symbols can each sit at their cap and no check objects.
    #
    # Both caps are ENTRY caps: the engine never trims a position that drifts
    # past its limit on appreciation alone, so gross can exceed the ceiling
    # without a single order having broken a rule. A de-risking order must
    # therefore ALWAYS be allowed through — otherwise an oversized book would
    # have no way back down, which is the opposite of what a risk limit is for.
    current_gross = _gross_exposure(ib, account)
    current_symbol_value = abs(current_qty) * price
    resulting_gross = current_gross - current_symbol_value + resulting_notional
    reduces_exposure = resulting_gross < current_gross - 1e-9
    over_limit = resulting_gross > settings.max_gross_notional
    result.add(
        "gross-exposure",
        not over_limit or reduces_exposure,
        f"resulting gross ${resulting_gross:,.2f} vs limit "
        f"${settings.max_gross_notional:,.2f}"
        + (" — allowed because it reduces exposure" if over_limit and reduces_exposure else ""),
    )

    open_orders = len(ib.reqAllOpenOrders())
    result.add(
        "open-orders",
        open_orders < settings.max_open_orders,
        f"{open_orders} open vs limit {settings.max_open_orders}",
    )

    daily = _daily_pnl(ib, account)
    if daily is None:
        result.add("daily-loss", True, "daily P&L unavailable (no positions yet?) — skipped")
    else:
        result.add(
            "daily-loss",
            daily > -settings.max_daily_loss,
            f"today ${daily:,.2f} vs max loss -${settings.max_daily_loss:,.2f}",
        )

    if settings.is_live:
        result.add(
            "live-ack",
            settings.live_ack_present,
            "LIVE_TRADING_ACK env var must be set by a human for live orders",
        )

    return result
