"""Alpaca broker adapter.

The point of this module is that trading/risk.py does NOT change. The risk
engine is duck-typed: it only needs an object exposing positions(), portfolio(),
reqAllOpenOrders(), reqPnL(), cancelPnL() and sleep(). This adapter presents
exactly that surface on top of Alpaca's REST API, so the same risk limits, the
same kill switch and the same restricted-symbol list govern both brokers.

The dataclasses below deliberately mimic the ib_async shapes (contract.symbol,
marketValue, order.lmtPrice, orderStatus.filled...) so trading/portfolio.py's
report tables work unchanged too.

Raw REST on the stdlib rather than alpaca-py: no extra dependency, and for a
system that moves money it is worth being able to read the exact HTTP call.

Credentials come from the environment and are never logged:
    ALPACA_API_KEY, ALPACA_SECRET_KEY
Optional:
    ALPACA_DATA_FEED   iex (free, default) or sip (paid subscription)

Paper vs live is decided ONLY by TRADING_MODE, which picks the base URL. Alpaca
issues separate key pairs for paper and live, so paper keys simply fail to
authenticate against the live endpoint — a stronger separation than IBKR's,
where one TWS can serve both and only the account prefix distinguishes them.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field

PAPER_BASE = "https://paper-api.alpaca.markets"
LIVE_BASE = "https://api.alpaca.markets"
DATA_BASE = "https://data.alpaca.markets"


# --- ib_async-shaped value objects ------------------------------------------

@dataclass
class Contract:
    symbol: str
    exchange: str = "ALPACA"
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


class AlpacaError(SystemExit):
    """Surfaced to the CLI as a clean message, never a traceback."""


# --- the adapter -------------------------------------------------------------

class AlpacaBroker:
    """Presents the slice of the ib_async IB interface that risk.py and
    portfolio.py rely on, backed by Alpaca REST."""

    def __init__(self, mode: str = "paper", timeout: int = 20) -> None:
        self.mode = mode
        self.timeout = timeout
        self.base = LIVE_BASE if mode == "live" else PAPER_BASE
        self.feed = os.environ.get("ALPACA_DATA_FEED", "iex")

        key = os.environ.get("ALPACA_API_KEY", "")
        secret = os.environ.get("ALPACA_SECRET_KEY", "")
        if not key or not secret:
            raise AlpacaError(
                "ALPACA_API_KEY / ALPACA_SECRET_KEY are not set.\n"
                "Create paper keys at https://app.alpaca.markets (Paper Trading -> "
                "API Keys) and put them in .env — a human edits that file, never an "
                "agent. Paper and live keys are separate; use the paper pair."
            )
        self._headers = {
            "APCA-API-KEY-ID": key,
            "APCA-API-SECRET-KEY": secret,
            "Content-Type": "application/json",
        }
        self._account_cache: dict | None = None

    # -- plumbing ------------------------------------------------------------

    def _request(self, method: str, url: str, body: dict | None = None) -> dict | list:
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method, headers=self._headers)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read().decode()
                return json.loads(raw) if raw.strip() else {}
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = json.loads(exc.read().decode()).get("message", "")
            except Exception:
                pass
            if exc.code in (401, 403):
                raise AlpacaError(
                    f"Alpaca rejected the credentials ({exc.code}). "
                    f"{detail}\nCheck ALPACA_API_KEY / ALPACA_SECRET_KEY, and that "
                    f"they are {self.mode.upper()} keys — paper and live pairs are "
                    "not interchangeable."
                ) from exc
            raise AlpacaError(
                f"Alpaca {method} {urllib.parse.urlparse(url).path} failed "
                f"({exc.code}): {detail or exc.reason}"
            ) from exc
        except urllib.error.URLError as exc:
            raise AlpacaError(
                f"Could not reach Alpaca ({exc.reason}). Check network connectivity."
            ) from exc

    def _trading(self, method: str, path: str, body: dict | None = None,
                 query: dict | None = None):
        url = f"{self.base}{path}"
        if query:
            url += "?" + urllib.parse.urlencode(query)
        return self._request(method, url, body)

    def _data(self, path: str, query: dict | None = None):
        url = f"{DATA_BASE}{path}"
        if query:
            url += "?" + urllib.parse.urlencode(query)
        return self._request("GET", url)

    # -- identity / connection ----------------------------------------------

    def account(self, refresh: bool = False) -> dict:
        if self._account_cache is None or refresh:
            self._account_cache = self._trading("GET", "/v2/account")
        return self._account_cache

    def connect_and_verify(self) -> str:
        """Authenticate and confirm the endpoint matches TRADING_MODE."""
        acct = self.account(refresh=True)
        account_id = acct.get("account_number") or acct.get("id", "unknown")
        if acct.get("status") and acct["status"] != "ACTIVE":
            raise AlpacaError(
                f"Alpaca account {account_id} status is {acct['status']}, not ACTIVE. "
                "Refusing to trade."
            )
        if acct.get("trading_blocked"):
            raise AlpacaError(f"Alpaca account {account_id} has trading_blocked=true.")
        return account_id

    def is_market_open(self) -> bool:
        try:
            return bool(self._trading("GET", "/v2/clock").get("is_open"))
        except AlpacaError:
            return False

    # -- the interface risk.py duck-types -----------------------------------

    def positions(self, account: str = "") -> list[Position]:
        rows = self._trading("GET", "/v2/positions")
        return [Position(Contract(r["symbol"]), float(r["qty"])) for r in rows]

    def portfolio(self, account: str = "") -> list[PortfolioItem]:
        rows = self._trading("GET", "/v2/positions")
        return [
            PortfolioItem(
                contract=Contract(r["symbol"]),
                position=float(r["qty"]),
                marketValue=float(r["market_value"]),
                averageCost=float(r["avg_entry_price"]),
                marketPrice=float(r.get("current_price") or 0.0),
                unrealizedPNL=float(r.get("unrealized_pl") or 0.0),
                realizedPNL=0.0,  # Alpaca does not expose per-position realized P&L
            )
            for r in rows
        ]

    def reqAllOpenOrders(self) -> list[Trade]:
        rows = self._trading("GET", "/v2/orders", query={"status": "open", "limit": 500})
        trades = []
        for r in rows:
            qty = float(r.get("qty") or 0)
            filled = float(r.get("filled_qty") or 0)
            trades.append(
                Trade(
                    contract=Contract(r["symbol"]),
                    order=Order(
                        orderId=r["id"][:8],
                        action=r["side"].upper(),
                        totalQuantity=qty,
                        orderType={"limit": "LMT", "market": "MKT"}.get(
                            r["type"], r["type"].upper()
                        ),
                        lmtPrice=float(r.get("limit_price") or 0.0),
                        tif=r.get("time_in_force", "day").upper(),
                    ),
                    orderStatus=OrderStatus(
                        status=r["status"],
                        filled=filled,
                        remaining=qty - filled,
                        avgFillPrice=float(r.get("filled_avg_price") or 0.0),
                    ),
                )
            )
        return trades

    def reqPnL(self, account: str = "", modelCode: str = "") -> PnL:
        """Alpaca gives equity and last_equity; daily P&L is the difference."""
        acct = self.account(refresh=True)
        try:
            daily = float(acct["equity"]) - float(acct["last_equity"])
        except (KeyError, TypeError, ValueError):
            daily = None
        unreal = sum(i.unrealizedPNL for i in self.portfolio()) if daily is not None else None
        return PnL(dailyPnL=daily, unrealizedPnL=unreal, realizedPnL=None)

    def cancelPnL(self, account: str = "", modelCode: str = "") -> None:
        pass  # no subscription to tear down

    def sleep(self, seconds: float) -> None:
        time.sleep(min(seconds, 0.1))  # REST is synchronous; no need to really wait

    def accountSummary(self, account: str = "") -> list[AccountValue]:
        acct = self.account(refresh=True)
        mapping = [
            ("NetLiquidation", "equity"),
            ("TotalCashValue", "cash"),
            ("BuyingPower", "buying_power"),
            ("GrossPositionValue", "long_market_value"),
            ("AvailableFunds", "cash"),
            ("MaintMarginReq", "maintenance_margin"),
        ]
        out = []
        for tag, key in mapping:
            if acct.get(key) is not None:
                out.append(AccountValue(tag, str(acct[key]), acct.get("currency", "USD")))
        return out

    # -- pricing -------------------------------------------------------------

    def reference_price(self, symbol: str) -> float:
        """Best available price for risk sizing.

        trading/market_data.reference_price() dispatches here when the broker
        provides it, which is how risk.py stays broker-agnostic without being
        modified. Order: last trade, then quote mid, then the most recent daily
        bar (stale — warns loudly, exactly as the IBKR path does).
        """
        symbol = symbol.upper()

        try:
            trade = self._data(f"/v2/stocks/{symbol}/trades/latest", {"feed": self.feed})
            price = float(trade.get("trade", {}).get("p") or 0)
            if price > 0:
                return price
        except AlpacaError:
            pass

        try:
            quote = self._data(f"/v2/stocks/{symbol}/quotes/latest", {"feed": self.feed})
            q = quote.get("quote", {})
            bid, ask = float(q.get("bp") or 0), float(q.get("ap") or 0)
            if bid > 0 and ask > 0:
                return (bid + ask) / 2
        except AlpacaError:
            pass

        bars = self.historical_bars(symbol, limit=5)
        if bars:
            last = bars[-1]
            print(
                f"WARNING: no live quote for {symbol} (market closed, or the "
                f"{self.feed.upper()} feed has no recent print). Sizing off the "
                f"{last['t'][:10]} close ${float(last['c']):,.2f}. This price is "
                "STALE — pass --limit for an accurate size."
            )
            return float(last["c"])

        raise AlpacaError(
            f"No usable price for {symbol}; cannot size the order safely. "
            "No trade, no quote and no bars available."
        )

    def snapshot(self, symbols: list[str]) -> list[dict]:
        rows = []
        for symbol in symbols:
            symbol = symbol.upper()
            bid = ask = last = None
            try:
                q = self._data(f"/v2/stocks/{symbol}/quotes/latest",
                               {"feed": self.feed}).get("quote", {})
                bid = float(q.get("bp") or 0) or None
                ask = float(q.get("ap") or 0) or None
            except AlpacaError:
                pass
            try:
                t = self._data(f"/v2/stocks/{symbol}/trades/latest",
                               {"feed": self.feed}).get("trade", {})
                last = float(t.get("p") or 0) or None
            except AlpacaError:
                pass
            rows.append({"symbol": symbol, "bid": bid, "ask": ask, "last": last})
        return rows

    def historical_bars(self, symbol: str, timeframe: str = "1Day",
                        limit: int = 100) -> list[dict]:
        payload = self._data(
            f"/v2/stocks/{symbol.upper()}/bars",
            {"timeframe": timeframe, "limit": limit, "feed": self.feed,
             "adjustment": "all"},
        )
        return payload.get("bars") or []

    # -- order entry ---------------------------------------------------------

    def place_order(self, symbol: str, side: str, quantity: float,
                    limit_price: float | None = None, tif: str = "day") -> Trade:
        body = {
            "symbol": symbol.upper(),
            "qty": str(quantity),
            "side": side.lower(),
            "type": "limit" if limit_price else "market",
            "time_in_force": tif.lower(),
        }
        if limit_price:
            body["limit_price"] = str(limit_price)
        r = self._trading("POST", "/v2/orders", body=body)
        qty = float(r.get("qty") or 0)
        filled = float(r.get("filled_qty") or 0)
        return Trade(
            contract=Contract(r["symbol"]),
            order=Order(
                orderId=r["id"][:8], action=r["side"].upper(), totalQuantity=qty,
                orderType="LMT" if limit_price else "MKT",
                lmtPrice=float(r.get("limit_price") or 0.0),
                tif=r.get("time_in_force", "day").upper(),
            ),
            orderStatus=OrderStatus(
                status=r["status"], filled=filled, remaining=qty - filled,
                avgFillPrice=float(r.get("filled_avg_price") or 0.0),
            ),
        )

    def cancel_all(self) -> int:
        open_orders = self.reqAllOpenOrders()
        if not open_orders:
            return 0
        self._trading("DELETE", "/v2/orders")
        return len(open_orders)

    def close_position(self, symbol: str) -> dict:
        return self._trading("DELETE", f"/v2/positions/{symbol.upper()}")
