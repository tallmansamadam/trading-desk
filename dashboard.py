#!/usr/bin/env python
"""dashboard.py — a Commodore 64 style live monitor for the trading desk.

Serves a local web UI that polls Alpaca and shows the account, the risk limits,
positions, open orders, and a zoomable price chart. Everything refreshes on a
timer and changed values flash, so a glance tells you it is still running.

  python dashboard.py                 # http://127.0.0.1:6400
  python dashboard.py --port 8080 --symbol QQQ

Read-only. It never places, cancels or modifies an order — it is a window onto
the desk, not a control panel. Order entry stays in trade_alpaca.py where the
risk engine and the permission rules live.
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from trading.brokers.alpaca import AlpacaBroker, AlpacaError
from trading.config import load_settings, trading_halted

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


# --- cached broker access ----------------------------------------------------

class Feed:
    """Polls Alpaca behind a short cache so the UI can refresh briskly without
    burning through the rate limit."""

    STATE_TTL = 2.0
    BARS_TTL = 20.0

    def __init__(self, broker: AlpacaBroker, settings) -> None:
        self.broker = broker
        self.settings = settings
        self._lock = threading.Lock()
        self._state: tuple[float, dict] | None = None
        self._bars: dict[tuple[str, str], tuple[float, list]] = {}
        self.ticks = 0

    def state(self) -> dict:
        with self._lock:
            now = time.time()
            if self._state and now - self._state[0] < self.STATE_TTL:
                return self._state[1]
            payload = self._build_state()
            self._state = (now, payload)
            self.ticks += 1
            payload["ticks"] = self.ticks
            return payload

    def _build_state(self) -> dict:
        s = self.settings
        out = {
            "ok": True, "error": None,
            "mode": s.mode.upper(),
            "halted": trading_halted(),
            "server_time": time.strftime("%H:%M:%S"),
            "limits": {
                "order": s.max_order_notional,
                "position": s.max_position_notional,
                "gross": getattr(s, "max_gross_notional", None),
                "daily_loss": s.max_daily_loss,
                "open_orders": s.max_open_orders,
                "restricted": list(s.restricted_symbols),
            },
        }
        try:
            acct = self.broker.account(refresh=True)
            equity = float(acct.get("equity") or 0)
            last_equity = float(acct.get("last_equity") or equity)
            out["account"] = {
                "id": acct.get("account_number", "?"),
                "equity": equity,
                "cash": float(acct.get("cash") or 0),
                "buying_power": float(acct.get("buying_power") or 0),
                "daily_pnl": equity - last_equity,
                "market_open": self.broker.is_market_open(),
            }
            items = self.broker.portfolio()
            gross = sum(abs(i.marketValue) for i in items)
            out["positions"] = [
                {"symbol": i.contract.symbol, "qty": i.position,
                 "avg": i.averageCost, "price": i.marketPrice,
                 "value": i.marketValue, "upnl": i.unrealizedPNL,
                 "pct_cap": (abs(i.marketValue) / s.max_position_notional * 100)
                            if s.max_position_notional else 0}
                for i in items
            ]
            out["gross"] = gross
            out["orders"] = [
                {"id": t.order.orderId, "symbol": t.contract.symbol,
                 "side": t.order.action, "qty": t.order.totalQuantity,
                 "type": t.order.orderType, "limit": t.order.lmtPrice,
                 "filled": t.orderStatus.filled, "status": t.orderStatus.status}
                for t in self.broker.reqAllOpenOrders()
            ]
        except AlpacaError as exc:
            out["ok"] = False
            out["error"] = str(exc).split("\n")[0]
            out.setdefault("account", {})
            out.setdefault("positions", [])
            out.setdefault("orders", [])
            out.setdefault("gross", 0)
        return out

    def bars(self, symbol: str, timeframe: str) -> list:
        key = (symbol.upper(), timeframe)
        with self._lock:
            now = time.time()
            hit = self._bars.get(key)
            if hit and now - hit[0] < self.BARS_TTL:
                return hit[1]
        try:
            limit = {"1Min": 390, "5Min": 300, "15Min": 260,
                     "1Hour": 300, "1Day": 400}.get(timeframe, 300)
            raw = self.broker.historical_bars(symbol, timeframe, limit)
            data = [{"t": b["t"], "o": b["o"], "h": b["h"], "l": b["l"],
                     "c": b["c"], "v": b["v"]} for b in raw]
        except AlpacaError:
            data = []
        with self._lock:
            self._bars[key] = (time.time(), data)
        return data


# --- the page ----------------------------------------------------------------

UI_FILE = Path(__file__).resolve().parent / "dashboard_ui.html"


def load_page() -> str:
    """Read the UI from disk on every request.

    Deliberately not cached: editing dashboard_ui.html and hitting refresh is
    the whole iteration loop for the look of this thing. The file is small and
    this server only ever talks to localhost.
    """
    try:
        return UI_FILE.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ("<pre>dashboard_ui.html is missing next to dashboard.py — "
                "the dashboard cannot render without it.</pre>")


class Handler(BaseHTTPRequestHandler):
    feed: Feed = None            # set on the class before serving
    default_symbol: str = "SPY"

    def _send(self, code, body, ctype):
        data = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        parsed = urlparse(self.path)
        try:
            if parsed.path in ("/", "/index.html"):
                page = load_page()
                if self.default_symbol != "SPY":
                    page = page.replace('id="sym" value="SPY"',
                                        f'id="sym" value="{self.default_symbol}"')
                self._send(200, page, "text/html; charset=utf-8")
            elif parsed.path == "/api/state":
                self._send(200, json.dumps(self.feed.state()), "application/json")
            elif parsed.path == "/api/bars":
                q = parse_qs(parsed.query)
                symbol = (q.get("symbol") or ["SPY"])[0]
                timeframe = (q.get("timeframe") or ["1Day"])[0]
                self._send(200, json.dumps(self.feed.bars(symbol, timeframe)),
                           "application/json")
            else:
                self._send(404, "not found", "text/plain")
        except BrokenPipeError:
            pass
        except Exception as exc:  # never let one bad request kill the board
            self._send(500, json.dumps({"error": str(exc)}), "application/json")

    def log_message(self, *args):
        pass  # the console is for the banner, not a request log


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--port", type=int, default=6400)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--symbol", default="SPY", help="symbol the chart opens on")
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()

    settings = load_settings()
    broker = AlpacaBroker(mode=settings.mode)
    account = broker.connect_and_verify()

    Handler.feed = Feed(broker, settings)
    Handler.default_symbol = args.symbol.upper()

    url = f"http://{args.host}:{args.port}"
    print("    **** CLAUDE TRADING DESK 64 ****")
    print(f"    ACCOUNT {account}   MODE {settings.mode.upper()}")
    print(f"    SERVING {url}")
    print("    READ-ONLY. CTRL+C TO STOP.")
    print("READY.")

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    if not args.no_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nSTOPPED.")
        server.shutdown()


if __name__ == "__main__":
    try:
        main()
    except AlpacaError as exc:
        sys.exit(str(exc))
