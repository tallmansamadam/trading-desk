#!/usr/bin/env python
"""run_desk.py — the autonomous intraday loop.

Runs a scalp strategy live against Alpaca paper: polls bars, computes the
signal, checks news sentiment as a veto, sends every intended order through the
SAME trading/risk.py the CLIs use, manages exits, and goes flat before the bell.

    python run_desk.py --symbol SPY                 # SHADOW: decides, sends nothing
    python run_desk.py --symbol SPY --arm           # actually places paper orders

SHADOW IS THE DEFAULT AND THAT IS DELIBERATE. As of the last evaluation the
bundled scalp signal had NO measurable edge: mean +0.248 bp/trade on SPY over
86 trades, standard error 0.714 bp, t = 0.35, p = 0.73. That is noise. Reaching
even t = 2 at that effect size would take roughly 2,800 trades. Arming this
without a better signal is a machine for paying spread to sample randomness.
Run it in shadow, compare its decisions against what actually happened, and
only arm it when a signal earns it.

Hard rails, none of them optional:
  * PAPER ONLY. Live mode is refused outright, whatever the environment says.
  * Every order passes risk.check_order first — same limits, same restricted
    list, same kill switch as the CLIs.
  * A HALT file stops new entries immediately; exits are still allowed.
  * Caps on trades per session and consecutive losses.
  * Stops entering at the daily-loss limit.
  * Force-flat before the close. A scalper holding overnight is a bug.
"""

from __future__ import annotations

import argparse
import csv
import signal as signal_mod
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from trading import risk
from trading.brokers.alpaca import AlpacaBroker, AlpacaError
from trading.config import load_settings, trading_halted
from trading.news import NewsFeed

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

LOG_DIR = Path(__file__).resolve().parent / "logs"
NEWLINE = chr(10)
_stop = False


def _handle_sigint(signum, frame):
    global _stop
    _stop = True
    print("\n[!] interrupt received — flattening and shutting down cleanly")


class Journal:
    """Append-only record of every decision, not just every fill."""

    FIELDS = ["ts", "event", "symbol", "side", "qty", "price", "detail"]

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.new = not path.exists()
        self.fh = open(path, "a", newline="", encoding="utf-8")
        self.w = csv.DictWriter(self.fh, fieldnames=self.FIELDS)
        if self.new:
            self.w.writeheader()

    def write(self, event, symbol="", side="", qty="", price="", detail=""):
        row = {"ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
               "event": event, "symbol": symbol, "side": side, "qty": qty,
               "price": price, "detail": detail}
        self.w.writerow(row)
        self.fh.flush()
        stamp = row["ts"][11:19]
        bits = " ".join(str(x) for x in (symbol, side, qty, price) if x != "")
        print(f"  {stamp}  {event:<14} {bits:<28} {detail}")

    def close(self):
        self.fh.close()


class Desk:
    def __init__(self, broker, settings, args) -> None:
        self.b = broker
        self.s = settings
        self.a = args
        self.symbol = args.symbol.upper()
        self.news = NewsFeed()
        # BTC/USD would otherwise become a directory separator in the path
        safe = self.symbol.replace("/", "-")
        self.journal = Journal(
            LOG_DIR / f"desk_{datetime.now().strftime('%Y%m%d')}_{safe}.csv")
        self.account = broker.connect_and_verify()

        self.spread_assumption = float(getattr(args, "spread_bp", 0.26))
        self.position = 0.0
        self.entry_px = 0.0
        self.entry_time = None
        self.trades = 0
        self.consecutive_losses = 0
        self.realized_bp = 0.0
        self.start_equity = float(broker.account(refresh=True).get("equity") or 0)

    # -- helpers ----------------------------------------------------------

    def bars(self, limit=120):
        return self.b.historical_bars(self.symbol, self.a.timeframe, limit)

    def signal_now(self, bars) -> tuple[bool, float | None]:
        import strategies.scalp_vwap as strat
        rows = [{"t": x["t"], "o": x["o"], "h": x["h"], "l": x["l"],
                 "c": x["c"], "v": x.get("v", 0)} for x in bars]
        z = strat.compute_vwap_z(rows, strat.PARAMS["lookback"], strat.PARAMS["warmup"])
        last = z[-1] if z else None
        return (last is not None and last <= -self.a.z_entry), last

    def minutes_to_close(self) -> float | None:
        if self.b.is_crypto(self.symbol):
            return float("inf")          # crypto never closes
        try:
            clock = self.b._trading("GET", "/v2/clock")
            if not clock.get("is_open"):
                return None
            close = datetime.fromisoformat(clock["next_close"].replace("Z", "+00:00"))
            now = datetime.fromisoformat(clock["timestamp"].replace("Z", "+00:00"))
            return (close - now).total_seconds() / 60
        except Exception:
            return None

    # -- order plumbing ---------------------------------------------------

    def submit(self, side: str, qty: float, limit_px: float, why: str) -> bool:
        """Risk-check then (only if armed) send. Returns True if it went."""
        verdict = risk.check_order(self.b, self.account, self.s,
                                   self.symbol, side, qty, limit_price=limit_px)
        if not verdict.approved:
            failed = [f"{n}: {d}" for n, p, d in verdict.checks if not p]
            self.journal.write("RISK-REJECT", self.symbol, side, qty,
                               f"{limit_px:.2f}", " | ".join(failed))
            return False
        if not self.a.arm:
            self.journal.write("SHADOW-ORDER", self.symbol, side, qty,
                               f"{limit_px:.2f}", f"{why} (not sent — shadow mode)")
            return True
        try:
            tr = self.b.place_order(self.symbol, side, qty, limit_px, "day")
        except AlpacaError as exc:
            self.journal.write("ORDER-ERROR", self.symbol, side, qty,
                               f"{limit_px:.2f}", str(exc)[:120])
            return False
        self.journal.write("ORDER-SENT", self.symbol, side, qty, f"{limit_px:.2f}",
                           f"{why} id={tr.order.orderId} status={tr.orderStatus.status}")
        return True

    def sync_position(self):
        if not self.a.arm:
            return
        try:
            held = next((p.position for p in self.b.positions()
                         if p.contract.symbol == self.symbol), 0.0)
        except AlpacaError:
            return
        self.position = float(held)

    def flatten(self, why: str):
        if self.position == 0:
            return
        px = self.b.reference_price(self.symbol)
        side = "SELL" if self.position > 0 else "BUY"
        qty = abs(self.position)
        # cross the spread to actually get out; getting flat beats getting a tick
        limit = px * (0.997 if side == "SELL" else 1.003)
        self.journal.write("EXIT", self.symbol, side, qty, f"{px:.2f}", why)
        if self.entry_px:
            # Entry is marked at a bar close, exit at the last trade. Those two
            # differ by about a spread even when nothing moved, which booked a
            # free gain on EVERY trade — the exact size of the edge being
            # measured. Charge the round trip so the estimate cannot flatter.
            raw = (px / self.entry_px - 1) * 10_000 * (1 if self.position > 0 else -1)
            bp = raw - self.spread_assumption
            self.realized_bp += bp
            self.consecutive_losses = 0 if bp > 0 else self.consecutive_losses + 1
            tag = "ESTIMATE" if not self.a.arm else "est. pre-fill"
            self.journal.write("TRADE-DONE", self.symbol, "", "", f"{px:.2f}",
                               f"{bp:+.2f} bp ({tag}: raw {raw:+.2f} less "
                               f"{self.spread_assumption:.2f} spread)  "
                               f"cumulative {self.realized_bp:+.2f} bp")
        self.submit(side, qty, limit, why)
        self.spread_assumption = float(getattr(args, "spread_bp", 0.26))
        self.position = 0.0
        self.entry_px = 0.0
        self.entry_time = None

    # -- the loop ---------------------------------------------------------

    def tick(self) -> bool:
        """One pass. Returns False when the session should end."""
        if trading_halted():
            self.journal.write("HALT", detail="HALT file present — flattening, then stopping")
            self.flatten("kill switch")
            return False

        mins = self.minutes_to_close()
        if mins is None:
            self.journal.write("CLOSED", detail="market shut — idling")
            return True
        if mins != float("inf") and mins <= self.a.flat_before:
            if self.position:
                self.flatten(f"{mins:.1f} min to close — mandatory flat")
            self.journal.write("SESSION-END", detail=f"{mins:.1f} min to close, standing down")
            return False

        equity = float(self.b.account(refresh=True).get("equity") or 0)
        dd = equity - self.start_equity
        if dd <= -self.s.max_daily_loss:
            self.journal.write("LOSS-LIMIT", detail=f"down {dd:.2f} vs limit "
                               f"{self.s.max_daily_loss} — flattening and stopping")
            self.flatten("daily loss limit")
            return False

        self.sync_position()
        bars = self.bars()
        if len(bars) < 40:
            self.journal.write("THIN-DATA", detail=f"{len(bars)} bars — waiting")
            return True
        last = bars[-1]["c"]

        # -- manage an open position first
        if self.position:
            held_min = (time.time() - self.entry_time) / 60 if self.entry_time else 0
            move_bp = (last / self.entry_px - 1) * 10_000 if self.entry_px else 0
            if move_bp >= self.a.target_bp:
                self.flatten(f"target hit {move_bp:+.1f} bp")
            elif move_bp <= -self.a.stop_bp:
                self.flatten(f"stop hit {move_bp:+.1f} bp")
            elif held_min >= self.a.max_hold_min:
                self.flatten(f"timeout after {held_min:.1f} min ({move_bp:+.1f} bp)")
            return True

        # -- otherwise consider an entry
        if self.trades >= self.a.max_trades:
            self.journal.write("TRADE-CAP", detail=f"{self.trades} trades — no more today")
            return True
        if self.consecutive_losses >= self.a.max_consecutive_losses:
            self.journal.write("LOSS-STREAK", detail=f"{self.consecutive_losses} in a row — "
                               "standing down for the session")
            return False

        fire, z = self.signal_now(bars)
        if not fire:
            return True

        vetoed, why = self.news.risk_veto(self.symbol, self.a.news_veto)
        if vetoed:
            self.journal.write("NEWS-VETO", self.symbol, detail=why)
            return True

        qty = max(1, int(self.a.notional // last))
        limit = last * 1.0005                       # small marketable offset
        self.journal.write("SIGNAL", self.symbol, "BUY", qty, f"{last:.2f}",
                           f"z={z:+.2f} | {why}")
        if self.submit("BUY", qty, limit, f"vwap z={z:+.2f}"):
            self.trades += 1
            self.position = qty
            self.entry_px = last
            self.entry_time = time.time()
        return True

    def run(self):
        print(f"  account {self.account}  symbol {self.symbol}  "
              f"mode {'ARMED — SENDING ORDERS' if self.a.arm else 'SHADOW — nothing sent'}")
        print(f"  notional/trade ${self.a.notional:,.0f}  target {self.a.target_bp}bp  "
              f"stop {self.a.stop_bp}bp  max {self.a.max_trades} trades")
        print(f"  journal {self.journal.path}")
        print()
        self.journal.write("START", self.symbol,
                           detail=f"{'ARMED' if self.a.arm else 'SHADOW'} "
                                  f"equity={self.start_equity:.2f}")
        try:
            while not _stop:
                try:
                    if not self.tick():
                        break
                except AlpacaError as exc:
                    self.journal.write("BROKER-ERROR", detail=str(exc)[:140])
                    time.sleep(5)
                except Exception as exc:                      # never die mid-position
                    self.journal.write("ERROR", detail=f"{type(exc).__name__}: {exc}"[:140])
                    time.sleep(5)
                time.sleep(self.a.interval)
        finally:
            if self.position:
                try:
                    self.flatten("shutdown")
                except Exception as exc:
                    self.journal.write("SHUTDOWN-FLATTEN-FAILED", detail=str(exc)[:140],
                                       side="CHECK POSITIONS MANUALLY")
            note = "ESTIMATED, not broker-confirmed" if not self.a.arm else                    "estimated pre-fill — reconcile against the broker"
            self.journal.write("STOP", detail=f"{self.trades} trades, "
                               f"{self.realized_bp:+.2f} bp ({note})")
            if self.a.arm:
                try:
                    eq = float(self.b.account(refresh=True).get("equity") or 0)
                    self.journal.write("RECONCILE", detail=
                        f"equity {self.start_equity:.2f} -> {eq:.2f} "
                        f"= {eq - self.start_equity:+.2f} actual")
                except Exception:
                    pass
            self.journal.close()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--symbol", default="SPY")
    ap.add_argument("--timeframe", default="1Min")
    ap.add_argument("--interval", type=float, default=20.0, help="seconds between passes")
    ap.add_argument("--notional", type=float, default=2000.0, help="$ per entry")
    ap.add_argument("--z-entry", type=float, default=2.0)
    ap.add_argument("--target-bp", type=float, default=10.0)
    ap.add_argument("--stop-bp", type=float, default=8.0)
    ap.add_argument("--max-hold-min", type=float, default=20.0)
    ap.add_argument("--max-trades", type=int, default=15)
    ap.add_argument("--max-consecutive-losses", type=int, default=4)
    ap.add_argument("--flat-before", type=float, default=10.0,
                    help="minutes before the close to force flat")
    ap.add_argument("--news-veto", type=float, default=-0.25)
    ap.add_argument("--spread-bp", type=float, default=0.26,
                    help="round-trip spread charged against estimated P&L")
    ap.add_argument("--arm", action="store_true",
                    help="actually send orders. Without this it only decides and logs.")
    args = ap.parse_args()

    settings = load_settings()
    if settings.is_live:
        sys.exit("REFUSED: run_desk.py is paper-only. An unattended loop will not "
                 "trade a live account, whatever TRADING_MODE says.")

    print("=" * 72)
    print("  AUTONOMOUS DESK")
    if args.arm:
        print("  ARMED — this will place real paper orders without asking again.")
        print("  Reminder: the bundled scalp signal showed NO measurable edge")
        print("  (mean +0.248 bp/trade, t = 0.35, p = 0.73 over 86 trades).")
    else:
        print("  SHADOW MODE — decisions are logged, no orders are sent.")
        print("  Add --arm once a signal has earned it.")
    print("=" * 72)

    signal_mod.signal(signal_mod.SIGINT, _handle_sigint)
    broker = AlpacaBroker(mode=settings.mode)

    # Cost sanity. A target narrower than the round trip loses on every trade
    # no matter how good the signal is — it is arithmetic, not strategy. Crypto
    # on this venue runs 26-84 bp round trip against a 2-5 bp typical minute,
    # which is how a "scalp BTC" instruction quietly becomes a shredder.
    live_spread = broker.spread_bp(args.symbol.upper())
    if live_spread is not None:
        print(f"  live round-trip spread on {args.symbol.upper()}: {live_spread:.2f} bp"
              f"   target: {args.target_bp:.2f} bp")
        if args.target_bp <= live_spread:
            msg = NEWLINE.join([
                f"REFUSED: target {args.target_bp:.2f} bp is not larger than the "
                f"{live_spread:.2f} bp round-trip spread on {args.symbol.upper()}.",
                "Every trade would start behind by more than it aims to make.",
                f"Widen --target-bp above {live_spread:.2f}, or pick a cheaper instrument.",
            ])
            if args.arm:
                sys.exit(msg)
            for ln in msg.split(NEWLINE):
                print("  [!] " + ln)
            print("  running in shadow anyway so you can watch it lose on paper.")
        args.spread_bp = live_spread          # book estimates at the real cost
    print("=" * 72)

    Desk(broker, settings, args).run()


if __name__ == "__main__":
    try:
        main()
    except AlpacaError as exc:
        sys.exit(str(exc))
