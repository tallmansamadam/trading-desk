"""Replay broker — the full broker surface, backed by history and a fake clock.

`simulate.py` replays history through the risk engine, which is useful but
leaves a band of production code untested: the Allocator's sizing, slicing,
in-flight and settle logic; the Service's scheduler, market-clock arithmetic
and backoff; and the order lifecycle itself. Those run only live, and every
behavioural bug found in this system so far has lived in exactly that band.

This closes it. ReplayBroker presents the same surface as AlpacaBroker, so
`desk_service.Service` and `run_desk.Allocator` run against it UNMODIFIED. The
harness owns the clock, so a decade replays in seconds and nothing sleeps.

Fill model, stated because it decides the answer:
  * a BUY limit fills when the bar trades at or below it, at min(limit, open) —
    a gap through the limit fills at the better price, as it would live
  * a SELL limit fills when the bar trades at or above it, at max(limit, open)
  * market orders fill at the next bar's open
  * DAY orders expire at the end of the bar's session
  * fills are ALL-OR-NOTHING and assume infinite depth at the touch. Real
    partial fills and queue position are not modelled, so a replay is
    optimistic about execution and should never be read as a fill guarantee.

Nothing here touches a real account. It cannot: there is no network client.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta


@dataclass
class Contract:
    symbol: str
    exchange: str = "REPLAY"
    currency: str = "USD"


@dataclass
class Position:
    contract: Contract
    position: float


@dataclass
class PortfolioItem:
    contract: Contract
    position: float
    marketValue: float
    averageCost: float
    marketPrice: float
    unrealizedPNL: float
    realizedPNL: float = 0.0


@dataclass
class Order:
    orderId: str
    action: str
    totalQuantity: float
    orderType: str
    lmtPrice: float = 0.0
    tif: str = "DAY"


@dataclass
class OrderStatus:
    status: str
    filled: float = 0.0
    remaining: float = 0.0
    avgFillPrice: float = 0.0


@dataclass
class Trade:
    contract: Contract
    order: Order
    orderStatus: OrderStatus


@dataclass
class PnL:
    dailyPnL: float | None = None
    unrealizedPnL: float | None = None
    realizedPnL: float | None = None


@dataclass
class AccountValue:
    tag: str
    value: str
    currency: str = "USD"


@dataclass
class _Resting:
    oid: str
    symbol: str
    side: str
    qty: float
    limit: float | None
    placed_bar: int
    tif: str = "day"


@dataclass
class Fill:
    ts: str
    symbol: str
    side: str
    qty: float
    price: float
    oid: str


class ReplayError(SystemExit):
    pass


class ReplayBroker:
    """Historical bars behind the AlpacaBroker interface."""

    SESSION_MINUTES = 390          # 09:30-16:00

    def __init__(self, bars: dict[str, list[dict]], dates: list[str],
                 equity: float = 100_000.0, cost_bps: float = 0.0) -> None:
        if not dates:
            raise ReplayError("replay needs at least one bar")
        self.bars = bars
        self.dates = dates
        self.i = 0                                   # current bar index
        self.cost = cost_bps / 10_000
        self.start_equity = equity
        self.cash = equity
        self.shares: dict[str, float] = {}
        self.cost_basis: dict[str, float] = {}
        self.realized = 0.0
        self.resting: list[_Resting] = []
        self.fills: list[Fill] = []
        self.rejects: list[str] = []
        self._oid = 0
        self._minute = 0.0                           # minutes into the session
        self._equity_open = equity                   # equity at the session open
        self.equity_curve: list[tuple[str, float]] = []
        self.api_calls = 0

    # -- clock and stepping -------------------------------------------------

    @property
    def now(self) -> str:
        return self.dates[self.i]

    def _session_open(self) -> datetime:
        d = self.dates[self.i][:10]
        return datetime.fromisoformat(d).replace(hour=13, minute=30, tzinfo=UTC)

    def sim_seconds(self) -> float:
        """Monotonic seconds in SIMULATED time, so the service's backoff
        measures replay minutes rather than wall-clock ones."""
        return self.i * self.SESSION_MINUTES * 60 + self._minute * 60

    def set_minute(self, minute: float) -> None:
        """Move the simulated intra-session clock without advancing the bar."""
        self._minute = max(0.0, min(float(minute), self.SESSION_MINUTES))

    def advance(self) -> bool:
        """Next bar: mark the book, fill or expire resting orders. False at the end."""
        if self.i >= len(self.dates) - 1:
            return False
        # Alpaca's last_equity is the PREVIOUS session's close, and the
        # daily-loss check is computed against it. Setting the reference to the
        # current equity after marking made dailyPnL identically zero, which
        # meant no replay ever exercised the daily-loss rail at all.
        prev_close_equity = self.equity()
        self.i += 1
        self._minute = 0.0
        self._match()
        self._expire()
        self._equity_open = prev_close_equity
        self.equity_curve.append((self.now, self.equity()))
        return True

    def _bar(self, symbol: str, offset: int = 0) -> dict | None:
        series = self.bars.get(symbol)
        idx = self.i + offset
        if not series or idx < 0 or idx >= len(series):
            return None
        return series[idx]

    def _match(self) -> None:
        """Fill any resting order the current bar trades through."""
        still: list[_Resting] = []
        for r in self.resting:
            b = self._bar(r.symbol)
            if b is None:
                still.append(r)
                continue
            price = None
            if r.limit is None:                       # market: fill at the open
                price = b["o"]
            elif r.side == "BUY" and b["l"] <= r.limit:
                price = min(r.limit, b["o"])          # a gap down fills better
            elif r.side == "SELL" and b["h"] >= r.limit:
                price = max(r.limit, b["o"])
            if price is None:
                still.append(r)
                continue
            self._apply_fill(r, price)
        self.resting = still

    def _apply_fill(self, r: _Resting, price: float) -> None:
        signed = r.qty if r.side == "BUY" else -r.qty
        held = self.shares.get(r.symbol, 0.0)
        fee = abs(r.qty) * price * self.cost
        if r.side == "SELL" and held > 0:             # book the realised leg
            basis = self.cost_basis.get(r.symbol, price)
            self.realized += (price - basis) * min(r.qty, held)
        new = held + signed
        if new > 0:
            prev_basis = self.cost_basis.get(r.symbol, price)
            if signed > 0:
                self.cost_basis[r.symbol] = (prev_basis * held + price * r.qty) / new
        else:
            self.cost_basis.pop(r.symbol, None)
        self.shares[r.symbol] = new
        if abs(new) < 1e-9:
            self.shares.pop(r.symbol, None)
        self.cash -= signed * price + fee
        self.fills.append(Fill(self.now, r.symbol, r.side, r.qty, price, r.oid))

    def _expire(self) -> None:
        keep = []
        for r in self.resting:
            if r.tif.lower() == "day" and r.placed_bar < self.i:
                continue                              # a DAY order dies at the close
            keep.append(r)
        self.resting = keep

    # -- valuation ----------------------------------------------------------

    def price(self, symbol: str) -> float:
        b = self._bar(symbol)
        return float(b["c"]) if b else 0.0

    def equity(self) -> float:
        return self.cash + sum(q * self.price(s) for s, q in self.shares.items())

    # -- the AlpacaBroker surface -------------------------------------------

    def connect_and_verify(self) -> str:
        return "REPLAY000000"

    @staticmethod
    def is_crypto(symbol: str) -> bool:
        return "/" in symbol

    def account(self, refresh: bool = False) -> dict:
        self.api_calls += 1
        eq = self.equity()
        return {"account_number": "REPLAY000000", "status": "ACTIVE",
                "currency": "USD", "equity": str(eq),
                "last_equity": str(self._equity_open), "cash": str(self.cash),
                "buying_power": str(max(0.0, self.cash) * 4),
                "long_market_value": str(eq - self.cash),
                "maintenance_margin": "0", "trading_blocked": False}

    def accountSummary(self, account: str = "") -> list[AccountValue]:
        eq = self.equity()
        return [AccountValue("NetLiquidation", str(eq)),
                AccountValue("TotalCashValue", str(self.cash)),
                AccountValue("BuyingPower", str(max(0.0, self.cash) * 4)),
                AccountValue("GrossPositionValue", str(eq - self.cash))]

    def positions(self, account: str = "") -> list[Position]:
        self.api_calls += 1
        return [Position(Contract(s), q) for s, q in self.shares.items() if q]

    def portfolio(self, account: str = "") -> list[PortfolioItem]:
        self.api_calls += 1
        out = []
        for s, q in self.shares.items():
            if not q:
                continue
            px = self.price(s)
            basis = self.cost_basis.get(s, px)
            out.append(PortfolioItem(Contract(s), q, q * px, basis, px,
                                     (px - basis) * q))
        return out

    def reqAllOpenOrders(self) -> list[Trade]:
        self.api_calls += 1
        return [Trade(Contract(r.symbol),
                      Order(r.oid, r.side, r.qty,
                            "LMT" if r.limit else "MKT", r.limit or 0.0, r.tif.upper()),
                      OrderStatus("accepted", 0.0, r.qty, 0.0))
                for r in self.resting]

    def reqPnL(self, account: str = "", modelCode: str = "") -> PnL:
        self.api_calls += 1
        return PnL(dailyPnL=self.equity() - self._equity_open,
                   unrealizedPnL=sum(i.unrealizedPNL for i in self.portfolio()),
                   realizedPnL=self.realized)

    def cancelPnL(self, account: str = "", modelCode: str = "") -> None:
        pass

    def sleep(self, seconds: float) -> None:
        pass                                          # replay time is free

    def reference_price(self, symbol: str) -> float:
        px = self.price(symbol)
        if px <= 0:
            raise ReplayError(f"no price for {symbol} at {self.now}")
        return px

    def spread_bp(self, symbol: str) -> float | None:
        return 2.0                                    # a plausible liquid-ETF touch

    def snapshot(self, symbols: list[str]) -> list[dict]:
        return [{"symbol": s.upper(), "bid": self.price(s), "ask": self.price(s),
                 "last": self.price(s)} for s in symbols]

    def historical_bars(self, symbol: str, timeframe: str = "1Day",
                        limit: int = 100) -> list[dict]:
        series = self.bars.get(symbol.upper()) or []
        window = series[max(0, self.i - limit + 1): self.i + 1]
        return [{"t": self.dates[max(0, self.i - len(window) + 1 + k)],
                 "o": b["o"], "h": b["h"], "l": b["l"], "c": b["c"],
                 "v": b.get("v", 0)} for k, b in enumerate(window)]

    def place_order(self, symbol: str, side: str, quantity: float,
                    limit_price: float | None = None, tif: str = "day") -> Trade:
        self.api_calls += 1
        symbol = symbol.upper()
        if self._bar(symbol) is None:
            self.rejects.append(f"{self.now} {symbol}: no data")
            raise ReplayError(f"no data for {symbol}")
        self._oid += 1
        oid = f"R{self._oid:06d}"
        self.resting.append(_Resting(oid, symbol, side.upper(), float(quantity),
                                     float(limit_price) if limit_price else None,
                                     self.i, tif))
        return Trade(Contract(symbol),
                     Order(oid, side.upper(), float(quantity),
                           "LMT" if limit_price else "MKT", limit_price or 0.0, tif.upper()),
                     OrderStatus("accepted", 0.0, float(quantity), 0.0))

    def cancel_all(self) -> int:
        n = len(self.resting)
        self.resting = []
        return n

    def close_position(self, symbol: str) -> dict:
        symbol = symbol.upper()
        q = self.shares.get(symbol, 0.0)
        if q:
            self.place_order(symbol, "SELL" if q > 0 else "BUY", abs(q), None, "day")
        return {"symbol": symbol}

    def _trading(self, method: str, path: str, body=None, query=None):
        """Only the clock is reached this way; anything else is a real network
        call in production and must not silently succeed here."""
        self.api_calls += 1
        if path == "/v2/clock":
            open_t = self._session_open()
            now = open_t + timedelta(minutes=self._minute)
            close_t = open_t + timedelta(minutes=self.SESSION_MINUTES)
            nxt = open_t + timedelta(days=1)
            return {"timestamp": now.isoformat().replace("+00:00", "Z"),
                    "is_open": True,
                    "next_open": nxt.isoformat().replace("+00:00", "Z"),
                    "next_close": close_t.isoformat().replace("+00:00", "Z")}
        if path == "/v2/orders":
            return [{"id": r.oid, "symbol": r.symbol, "side": r.side.lower(),
                     "qty": str(r.qty), "status": "new",
                     "limit_price": str(r.limit or ""), "filled_qty": "0"}
                    for r in self.resting]
        raise ReplayError(f"replay broker has no endpoint {method} {path}")
