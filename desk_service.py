#!/usr/bin/env python
"""desk_service.py — the desk as a single installed program.

One process: a scheduler thread that trades, and an HTTP server that serves the
C64 front end and accepts control commands from it. Install it and it starts
with Windows, waits for the opening bell, and rebalances the book.

    python desk_service.py                  # run in the foreground
    python desk_service.py --no-browser     # how the installed task runs it

SAFETY, unchanged from the CLIs because it is the same code:
  * PAPER ONLY. Live mode is refused at startup, whatever the environment says.
  * Every order passes trading/risk.py first — same limits, same restricted
    list, same two-tier caps.
  * The HALT file stops new entries instantly and is reachable from the GUI.
  * ARMED is persisted state, shown on every screen, and one click to revoke.
    Disarmed, the service still watches and reports; it simply does not trade.

The scheduler deliberately waits a few minutes after the open before its first
action. Orders resting from a previous session fill at the bell, and acting
before they settle would double up on positions that are already arriving.
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
import time
import webbrowser
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from trading import risk
from trading.brokers.alpaca import AlpacaBroker, AlpacaError
from trading.config import HALT_FILE, load_settings, trading_halted

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent
LOG_DIR = ROOT / "logs"
UI_FILE = ROOT / "dashboard_ui.html"
STATE_FILE = LOG_DIR / "service_state.json"

DEFAULT_UNIVERSE = ["SPY", "QQQ", "IWM", "EFA", "EEM", "TLT",
                    "IEF", "LQD", "HYG", "GLD", "DBC", "VNQ"]


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class ServiceState:
    """Persisted across restarts, because ARMED is a decision the human made
    and a reboot is not a reason to silently forget it — or to silently keep
    it, which is why the GUI shows it on every screen."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.data = {"armed": False, "last_rebalance": None,
                     "last_action": None, "events": []}
        self.load()

    def load(self) -> None:
        try:
            self.data.update(json.loads(self.path.read_text(encoding="utf-8")))
        except (FileNotFoundError, ValueError):
            pass

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.data, indent=2), encoding="utf-8")

    def log(self, kind: str, detail: str) -> None:
        self.data["events"].insert(0, {"ts": now_iso(), "kind": kind, "detail": detail})
        del self.data["events"][60:]          # keep the panel bounded
        self.data["last_action"] = f"{kind}: {detail}"
        self.save()
        print(f"  {now_iso()[11:19]}  {kind:<14} {detail}")


class Service:
    STATE_TTL = 2.0
    BARS_TTL = 20.0

    def __init__(self, broker, settings, args) -> None:
        self.b = broker
        self.s = settings
        self.a = args
        self.symbols = [x.upper() for x in args.symbols]
        self.state = ServiceState(STATE_FILE)
        self.account = broker.connect_and_verify()
        self.lock = threading.Lock()
        self._snap: tuple[float, dict] | None = None
        self._bars: dict[tuple, tuple[float, list]] = {}
        self.allocator = self._make_allocator()

    def _make_allocator(self):
        """Reuse run_desk.Allocator as the strategy; the service owns timing."""
        import types
        import run_desk
        run_desk.LOG_DIR = LOG_DIR
        alloc_args = types.SimpleNamespace(
            symbols=self.symbols, invested=self.a.invested,
            rebalance_days=self.a.rebalance_days, drift_pct=self.a.drift_pct,
            min_trade=self.a.min_trade, once=True, arm=False, interval=60.0)
        a = run_desk.Allocator(self.b, self.s, alloc_args)
        a.STATE = LOG_DIR / "allocator_state.json"
        return a

    # -- market clock ------------------------------------------------------

    def clock(self) -> dict:
        try:
            return self.b._trading("GET", "/v2/clock")
        except AlpacaError:
            return {}

    def minutes_since_open(self) -> float | None:
        c = self.clock()
        if not c.get("is_open"):
            return None
        try:
            now = datetime.fromisoformat(c["timestamp"].replace("Z", "+00:00"))
            close = datetime.fromisoformat(c["next_close"].replace("Z", "+00:00"))
            # the session is 6.5h, so time-since-open is close minus 6.5h
            open_t = close.timestamp() - 6.5 * 3600
            return (now.timestamp() - open_t) / 60
        except (KeyError, ValueError):
            return None

    # -- the scheduler -----------------------------------------------------

    def tick(self) -> None:
        if not self.state.data["armed"]:
            return
        if trading_halted():
            return                       # HALT is checked again inside risk.py
        since_open = self.minutes_since_open()
        if since_open is None:
            return                       # market shut: nothing to do
        if since_open < self.a.open_delay:
            return                       # let resting orders fill before acting
        self.allocator.state = {"last_rebalance": self.state.data["last_rebalance"]}
        due, why = self.allocator.due()
        if not due:
            return
        self.state.log("REBALANCE", why)
        self.allocator.a.arm = True      # armed service means armed allocator
        try:
            self.allocator.rebalance()
        finally:
            self.allocator.a.arm = False
        self.state.data["last_rebalance"] = self.allocator.state.get("last_rebalance")
        self.state.save()

    def scheduler(self) -> None:
        while True:
            try:
                self.tick()
            except AlpacaError as exc:
                self.state.log("BROKER-ERROR", str(exc)[:120])
            except Exception as exc:
                self.state.log("ERROR", f"{type(exc).__name__}: {exc}"[:120])
            time.sleep(self.a.check_seconds)

    # -- controls ----------------------------------------------------------

    def control(self, action: str) -> dict:
        """Every mutating action the GUI can request. De-risking actions are
        always available; risk-increasing ones require ARMED."""
        try:
            if action == "halt":
                HALT_FILE.write_text("halted from the dashboard\n")
                self.state.log("HALT", "kill switch engaged — no new entries")
                return {"ok": True, "msg": "HALTED. New entries blocked."}

            if action == "resume":
                if HALT_FILE.exists():
                    HALT_FILE.unlink()
                self.state.log("RESUME", "kill switch cleared")
                return {"ok": True, "msg": "Resumed."}

            if action == "arm":
                self.state.data["armed"] = True
                self.state.log("ARM", "service will rebalance automatically")
                return {"ok": True, "msg": "ARMED. Will trade at the next opportunity."}

            if action == "disarm":
                self.state.data["armed"] = False
                self.state.log("DISARM", "service will watch but not trade")
                return {"ok": True, "msg": "Disarmed. Existing holdings untouched."}

            if action == "cancel_all":
                n = self.b.cancel_all()
                self.state.log("CANCEL-ALL", f"{n} order(s) cancelled")
                return {"ok": True, "msg": f"Cancelled {n} order(s)."}

            if action == "rebalance":
                if not self.state.data["armed"]:
                    return {"ok": False, "msg": "Refused: arm the desk first."}
                self.allocator.state = {"last_rebalance": None}   # force a pass
                self.allocator.a.arm = True
                try:
                    self.allocator.rebalance()
                finally:
                    self.allocator.a.arm = False
                self.state.data["last_rebalance"] = self.allocator.state.get("last_rebalance")
                self.state.log("REBALANCE", "manual, from the dashboard")
                return {"ok": True, "msg": "Rebalance pass complete — see the journal."}

            if action == "flatten":
                sold = 0
                for p in self.b.positions():
                    self.b.close_position(p.contract.symbol)
                    sold += 1
                self.state.log("FLATTEN", f"closed {sold} position(s)")
                return {"ok": True, "msg": f"Closing {sold} position(s)."}

            return {"ok": False, "msg": f"Unknown action {action!r}"}
        except AlpacaError as exc:
            self.state.log("CONTROL-ERROR", str(exc)[:120])
            return {"ok": False, "msg": str(exc)[:200]}

    # -- read models -------------------------------------------------------

    def snapshot(self) -> dict:
        with self.lock:
            now = time.time()
            if self._snap and now - self._snap[0] < self.STATE_TTL:
                return self._snap[1]
        payload = self._build()
        with self.lock:
            self._snap = (time.time(), payload)
        return payload

    def _build(self) -> dict:
        s = self.s
        clock = self.clock()
        out = {
            "ok": True, "error": None, "mode": s.mode.upper(),
            "halted": trading_halted(),
            "armed": self.state.data["armed"],
            "service": True,
            "last_action": self.state.data.get("last_action"),
            "last_rebalance": self.state.data.get("last_rebalance"),
            "events": self.state.data.get("events", [])[:12],
            "universe": self.symbols,
            "next_open": (clock.get("next_open") or "")[:16].replace("T", " "),
            "limits": {"order": s.max_order_notional,
                       "position": s.max_position_notional,
                       "gross": getattr(s, "max_gross_notional", None),
                       "daily_loss": s.max_daily_loss,
                       "open_orders": s.max_open_orders,
                       "restricted": list(s.restricted_symbols)},
        }
        try:
            acct = self.b.account(refresh=True)
            eq = float(acct.get("equity") or 0)
            out["account"] = {
                "id": acct.get("account_number", "?"), "equity": eq,
                "cash": float(acct.get("cash") or 0),
                "buying_power": float(acct.get("buying_power") or 0),
                "daily_pnl": eq - float(acct.get("last_equity") or eq),
                "market_open": bool(clock.get("is_open")),
            }
            items = self.b.portfolio()
            out["gross"] = sum(abs(i.marketValue) for i in items)
            out["positions"] = [
                {"symbol": i.contract.symbol, "qty": i.position,
                 "avg": i.averageCost, "price": i.marketPrice,
                 "value": i.marketValue, "upnl": i.unrealizedPNL,
                 "pct_cap": (abs(i.marketValue) / s.max_position_notional * 100)
                            if s.max_position_notional else 0}
                for i in items]
            out["orders"] = [
                {"id": t.order.orderId, "symbol": t.contract.symbol,
                 "side": t.order.action, "qty": t.order.totalQuantity,
                 "type": t.order.orderType, "limit": t.order.lmtPrice,
                 "filled": t.orderStatus.filled, "status": t.orderStatus.status}
                for t in self.b.reqAllOpenOrders()]
        except AlpacaError as exc:
            out.update({"ok": False, "error": str(exc).split("\n")[0],
                        "account": {}, "positions": [], "orders": [], "gross": 0})
        return out

    def bars(self, symbol: str, timeframe: str) -> list:
        key = (symbol.upper(), timeframe)
        with self.lock:
            hit = self._bars.get(key)
            if hit and time.time() - hit[0] < self.BARS_TTL:
                return hit[1]
        try:
            limit = {"1Min": 390, "5Min": 300, "15Min": 260,
                     "1Hour": 300, "1Day": 400}.get(timeframe, 300)
            raw = self.b.historical_bars(symbol, timeframe, limit)
            data = [{"t": b["t"], "o": b["o"], "h": b["h"], "l": b["l"],
                     "c": b["c"], "v": b.get("v", 0)} for b in raw]
        except AlpacaError:
            data = []
        with self.lock:
            self._bars[key] = (time.time(), data)
        return data


class Handler(BaseHTTPRequestHandler):
    svc: Service = None

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
                try:
                    page = UI_FILE.read_text(encoding="utf-8")
                except FileNotFoundError:
                    page = "<pre>dashboard_ui.html is missing.</pre>"
                self._send(200, page, "text/html; charset=utf-8")
            elif parsed.path == "/api/state":
                self._send(200, json.dumps(self.svc.snapshot()), "application/json")
            elif parsed.path == "/api/bars":
                q = parse_qs(parsed.query)
                self._send(200, json.dumps(self.svc.bars(
                    (q.get("symbol") or ["SPY"])[0],
                    (q.get("timeframe") or ["1Day"])[0])), "application/json")
            else:
                self._send(404, "not found", "text/plain")
        except BrokenPipeError:
            pass
        except Exception as exc:
            self._send(500, json.dumps({"error": str(exc)}), "application/json")

    def do_POST(self):
        parsed = urlparse(self.path)
        if parsed.path != "/api/control":
            self._send(404, "not found", "text/plain")
            return
        try:
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(n) or b"{}")
            result = self.svc.control(str(body.get("action", "")))
            self.svc._snap = None                    # force the UI to refresh
            self._send(200, json.dumps(result), "application/json")
        except Exception as exc:
            self._send(500, json.dumps({"ok": False, "msg": str(exc)}),
                       "application/json")

    def log_message(self, *args):
        pass


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", type=int, default=6400)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--symbols", nargs="+", default=DEFAULT_UNIVERSE)
    ap.add_argument("--invested", type=float, default=100.0)
    ap.add_argument("--rebalance-days", type=int, default=21)
    ap.add_argument("--drift-pct", type=float, default=25.0)
    ap.add_argument("--min-trade", type=float, default=200.0)
    ap.add_argument("--check-seconds", type=float, default=60.0)
    ap.add_argument("--open-delay", type=float, default=5.0,
                    help="minutes after the open before the first action")
    ap.add_argument("--no-browser", action="store_true")
    args = ap.parse_args()

    settings = load_settings()
    if settings.is_live:
        sys.exit("REFUSED: desk_service.py is paper-only. An unattended service "
                 "will not trade a live account, whatever TRADING_MODE says.")

    broker = AlpacaBroker(mode=settings.mode)
    svc = Service(broker, settings, args)
    Handler.svc = svc

    url = f"http://{args.host}:{args.port}"
    print("=" * 70)
    print("    **** CLAUDE TRADING DESK 64 — SERVICE ****")
    print(f"    ACCOUNT {svc.account}   MODE {settings.mode.upper()}")
    print(f"    ARMED   {svc.state.data['armed']}")
    print(f"    UNIVERSE {len(svc.symbols)} symbols")
    print(f"    SERVING {url}")
    print("=" * 70)
    print("READY.")

    threading.Thread(target=svc.scheduler, daemon=True).start()
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    if not args.no_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
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
