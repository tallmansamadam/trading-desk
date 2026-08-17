#!/usr/bin/env python
"""run_desk.py — the autonomous intraday loop.

Runs a scalp strategy live against Alpaca paper: polls bars, computes the
signal, checks news sentiment as a veto, sends every intended order through the
SAME trading/risk.py the CLIs use, manages exits, and goes flat before the bell.

    python run_desk.py                              # allocate, SHADOW (the default)
    python run_desk.py --once --arm                 # one rebalance, for real
    python run_desk.py --mode scalp --symbol SPY    # the intraday loop

Two modes, and they are not equals.

ALLOCATE equal-weights a universe and rebalances on a cadence. It forecasts
nothing, and it is the only thing here with evidence behind it: Sharpe 0.94
across 24 symbols over ten years against a median single-asset 0.58, beating 20
of the 24, at 0.46x annual turnover. It is the default for that reason.

SCALP runs the intraday signal loop. Its signal has no measurable edge
(+0.248 bp/trade, t = 0.35, p = 0.73) and fails all five gates in validate.py.
It is kept because the machinery is useful and a signal may one day earn it.

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
import json
import signal as signal_mod
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import ClassVar

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

    FIELDS: ClassVar[list[str]] = ["ts", "event", "symbol", "side",
                                   "qty", "price", "detail"]

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.new = not path.exists()
        # Deliberately long-lived: the journal stays open for the life of
        # the run and is closed in the finally block of run().
        self.fh = open(path, "a", newline="", encoding="utf-8")  # noqa: SIM115
        self.w = csv.DictWriter(self.fh, fieldnames=self.FIELDS)
        if self.new:
            self.w.writeheader()

    def write(self, event, symbol="", side="", qty="", price="", detail=""):
        row = {"ts": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
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



class Allocator:
    """Equal-weight the universe and rebalance on a cadence.

    This is the only thing in this repo with measured evidence behind it. Over
    24 symbols and ten years, equal weight rebalanced monthly ran at Sharpe 0.94
    against a median single-asset Sharpe of 0.58, with a shallower drawdown than
    19 of the 24, at 0.46x annual turnover. It beat 20 of the 24 outright.

    It forecasts nothing. There is no signal, no entry rule and no exit rule,
    only a target weight and a cadence. That is the point: every forecasting
    rule tried here failed validation and this did not, because it is not
    trying to predict anything.

    Sizing respects the two-tier caps rather than firing orders and collecting
    rejections. The book targets min(equity, MAX_GROSS_NOTIONAL) and each name
    is held to MAX_POSITION_NOTIONAL, so the risk engine confirms rather than
    corrects.
    """

    STATE = LOG_DIR / "allocator_state.json"

    def __init__(self, broker, settings, args) -> None:
        self.b = broker
        self.s = settings
        self.a = args
        self.symbols = [x.upper() for x in args.symbols]
        self.journal = Journal(
            LOG_DIR / f"alloc_{datetime.now().strftime('%Y%m%d')}.csv")
        self.account = broker.connect_and_verify()
        self.state = self._load_state()

    def _load_state(self) -> dict:
        try:
            return json.loads(self.STATE.read_text(encoding="utf-8"))
        except (FileNotFoundError, ValueError):
            return {"last_rebalance": None}

    def _save_state(self) -> None:
        if self.a.arm:            # a shadow run must not fake progress
            self.STATE.parent.mkdir(parents=True, exist_ok=True)
            self.STATE.write_text(json.dumps(self.state, indent=2), encoding="utf-8")

    # -- sizing -------------------------------------------------------------

    def budget(self):
        """(gross to deploy, per-symbol target) inside both caps."""
        equity = float(self.b.account(refresh=True).get("equity") or 0)
        gross_cap = getattr(self.s, "max_gross_notional", equity)
        gross = min(equity * self.a.invested / 100.0, gross_cap)
        per = min(gross / len(self.symbols), self.s.max_position_notional)
        return per * len(self.symbols), per

    def current_values(self):
        out = dict.fromkeys(self.symbols, 0.0)
        for item in self.b.portfolio():
            if item.contract.symbol in out:
                out[item.contract.symbol] = float(item.marketValue)
        return out

    def due(self):
        last = self.state.get("last_rebalance")
        today = datetime.now(UTC).date()
        if last is None:
            return True, "no prior rebalance recorded - establishing the book"
        gap = (today - datetime.fromisoformat(last).date()).days
        if gap >= self.a.rebalance_days:
            return True, f"{gap} days since the last rebalance"
        # drift trigger: cheaper than the calendar when one name runs away
        _, per = self.budget()
        held = self.current_values()
        if per > 0 and any(held.values()):
            worst = max(abs(v - per) / per for v in held.values())
            if worst >= self.a.drift_pct / 100.0:
                return True, f"a holding drifted {worst:.0%} from target"
        return False, f"{gap}d since rebalance, nothing past the drift band"

    # -- execution ----------------------------------------------------------

    def pending_symbols(self) -> set:
        """Symbols with an order already working.

        current_values() reads FILLED positions, so a resting order is
        invisible to the sizing maths. Left alone, an unattended loop sees the
        same shortfall on every pass and stacks a duplicate order each time.
        Anything already in flight is therefore skipped until it fills or
        expires."""
        try:
            return {t.contract.symbol for t in self.b.reqAllOpenOrders()}
        except AlpacaError:
            return set()          # unknown: fall through to the open-order cap

    def rebalance(self) -> None:
        gross, per = self.budget()
        held = self.current_values()
        pending = self.pending_symbols()
        if pending:
            self.journal.write("IN-FLIGHT", detail=f"skipping {len(pending)} name(s) "
                               f"with working orders: {' '.join(sorted(pending))}")
        self.journal.write("PLAN", detail=f"{len(self.symbols)} names, "
                           f"${per:,.0f} each, ${gross:,.0f} gross")

        orders = []
        for sym in self.symbols:
            if sym in pending:
                continue
            try:
                px = self.b.reference_price(sym)
            except AlpacaError as exc:
                self.journal.write("NO-PRICE", sym, detail=str(exc)[:90])
                continue
            if px <= 0:
                continue
            delta_usd = per - held.get(sym, 0.0)
            if abs(delta_usd) < self.a.min_trade:
                continue
            # A target position is routinely larger than MAX_ORDER_NOTIONAL
            # allows in one go, so slice it. The remainder is picked up on
            # later passes, which is how a desk scales into a position anyway.
            slice_usd = min(abs(delta_usd), self.s.max_order_notional * 0.97)
            qty = int(slice_usd // px)               # whole shares only
            if qty < 1:
                continue
            partial = qty * px < abs(delta_usd) * 0.95
            orders.append((sym, "BUY" if delta_usd > 0 else "SELL", qty, px, partial))

        if not orders:
            # Nothing actionable. Once whole-share rounding leaves every gap
            # under one share, the book is as close to target as this sizing
            # can get — that is DONE, not "try again in a minute". Returning
            # without banking the date made the service replay the same
            # impossible pass every 60 seconds indefinitely.
            self.journal.write("IN-BALANCE", detail="no gap is worth a whole share"
                               + (f"; {len(pending)} still in flight" if pending else ""))
            if not pending:
                self.state["last_rebalance"] = datetime.now(UTC).date().isoformat()
                self._save_state()
            return 0

        # Sells first. They free buying power and reduce gross, so a later buy
        # is less likely to trip the gross cap part-way through a rebalance.
        orders.sort(key=lambda o: 0 if o[1] == "SELL" else 1)

        # Leave headroom under MAX_OPEN_ORDERS. Anything not sent this pass is
        # sent on the next one; a monthly rebalance is in no hurry.
        try:
            already = len(self.b.reqAllOpenOrders())
        except AlpacaError:
            already = 0
        budget = max(0, self.s.max_open_orders - already - 1)
        throttled = max(0, len(orders) - budget)
        if throttled:
            self.journal.write("THROTTLE", detail=f"{len(orders)} adjustments needed, "
                               f"{budget} order slot(s) free — {throttled} follow next pass")
            orders = orders[:budget]

        sent = deferred = 0
        for sym, side, qty, px, partial in orders:
            verdict = risk.check_order(self.b, self.account, self.s,
                                       sym, side, qty, limit_price=px)
            if not verdict.approved:
                failed = [f"{n}: {d}" for n, p, d in verdict.checks if not p]
                self.journal.write("RISK-REJECT", sym, side, qty, f"{px:.2f}",
                                   " | ".join(failed))
                continue
            if partial:
                deferred += 1
            tag = " [slice]" if partial else ""
            if not self.a.arm:
                self.journal.write("SHADOW-ORDER", sym, side, qty, f"{px:.2f}",
                                   f"${qty*px:,.0f}{tag} (not sent - shadow mode)")
                sent += 1
                continue
            limit = round(px * (1.002 if side == "BUY" else 0.998), 2)
            try:
                tr = self.b.place_order(sym, side, qty, limit, "day")
                self.journal.write("ORDER-SENT", sym, side, qty, f"{limit:.2f}",
                                   f"${qty*px:,.0f}{tag} id={tr.order.orderId} "
                                   f"status={tr.orderStatus.status}")
                sent += 1
            except AlpacaError as exc:
                self.journal.write("ORDER-ERROR", sym, side, qty, f"{px:.2f}",
                                   str(exc)[:110])

        # Only bank the rebalance date once the book is actually at target.
        # Recording it after a sliced pass would stop the remainder ever going.
        if deferred == 0 and not pending:
            self.state["last_rebalance"] = datetime.now(UTC).date().isoformat()
            self._save_state()
        outstanding = []
        if deferred:
            outstanding.append(f"{deferred} sliced target(s)")
        if pending:
            outstanding.append(f"{len(pending)} order(s) in flight")
        if throttled:
            outstanding.append(f"{throttled} adjustment(s) throttled")
        self.journal.write("REBALANCED", detail=f"{sent} order(s) "
                           f"{'sent' if self.a.arm else 'simulated'}"
                           + (", still to do: " + ", ".join(outstanding)
                              if outstanding else ", book at target"))
        return sent

    def run(self) -> None:
        gross, per = self.budget()
        print(f"  account {self.account}   {len(self.symbols)} symbols")
        print(f"  mode {'ARMED - SENDING ORDERS' if self.a.arm else 'SHADOW - nothing sent'}")
        print(f"  target ${per:,.0f} per name, ${gross:,.0f} gross "
              f"(caps ${self.s.max_position_notional:,.0f}/name, "
              f"${getattr(self.s, 'max_gross_notional', 0):,.0f} book)")
        print(f"  rebalance every {self.a.rebalance_days}d or on {self.a.drift_pct}% drift")
        print(f"  journal {self.journal.path}")
        print()
        self.journal.write("START", detail="allocator, "
                           + ("ARMED" if self.a.arm else "SHADOW"))
        try:
            while not _stop:
                try:
                    if trading_halted():
                        self.journal.write("HALT", detail="HALT file present - no "
                                           "rebalancing. Existing holdings kept.")
                    else:
                        go, why = self.due()
                        self.journal.write("CHECK", detail=why)
                        if go:
                            self.rebalance()
                except AlpacaError as exc:
                    self.journal.write("BROKER-ERROR", detail=str(exc)[:140])
                except Exception as exc:
                    self.journal.write("ERROR", detail=f"{type(exc).__name__}: {exc}"[:140])
                if self.a.once:
                    break
                time.sleep(self.a.interval)
        finally:
            self.journal.write("STOP", detail="stopped - holdings left in place, "
                               "which is correct for a holding strategy")
            self.journal.close()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--mode", choices=["allocate", "scalp"], default="allocate",
                    help="allocate = equal-weight the universe (evidence-backed); "
                         "scalp = the intraday signal loop (no measured edge)")
    ap.add_argument("--symbol", default="SPY", help="scalp mode: the one symbol")
    ap.add_argument("--symbols", nargs="+", default=[
                        "SPY", "QQQ", "IWM", "EFA", "EEM",
                        "TLT", "IEF", "LQD", "HYG", "GLD", "DBC", "VNQ"],
                    help="allocate mode: the universe to equal-weight")
    ap.add_argument("--invested", type=float, default=100.0,
                    help="allocate mode: percent of equity to deploy, before caps")
    ap.add_argument("--rebalance-days", type=int, default=21)
    ap.add_argument("--drift-pct", type=float, default=25.0,
                    help="allocate mode: rebalance early if a name drifts this far")
    ap.add_argument("--min-trade", type=float, default=200.0,
                    help="allocate mode: skip adjustments smaller than this")
    ap.add_argument("--once", action="store_true",
                    help="run a single pass and exit (good for cron)")
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
    print(f"  AUTONOMOUS DESK — {args.mode.upper()}")
    if args.mode == "allocate":
        print("  Equal weight, rebalanced. Forecasts nothing.")
        print("  Measured: Sharpe 0.94 over 24 symbols / 10 years, against a")
        print("  median single-asset 0.58, at 0.46x turnover. Beat 20 of 24.")
    else:
        print("  Intraday signal loop.")
        print("  The bundled scalp signal has NO measurable edge: mean")
        print("  +0.248 bp/trade, t = 0.35, p = 0.73 over 86 trades. It also")
        print("  failed all five gates in validate.py. Shadow is the honest")
        print("  setting; --arm pays spread to sample noise.")
    if args.arm:
        print("  ARMED — this places real paper orders without asking again.")
    else:
        print("  SHADOW — decisions are logged, no orders are sent.")
    print("=" * 72)

    signal_mod.signal(signal_mod.SIGINT, _handle_sigint)
    broker = AlpacaBroker(mode=settings.mode)

    if args.mode == "allocate":
        if len(args.symbols) < 2:
            sys.exit("REFUSED: allocate mode needs at least 2 symbols. The whole "
                     "measured effect is diversification; one name is not a book.")
        Allocator(broker, settings, args).run()
        return

    # Cost sanity. A target narrower than the round trip loses on every trade
    # no matter how good the signal is — it is arithmetic, not strategy. Crypto
    # on this venue runs 26-84 bp round trip against a 2-5 bp typical minute,
    # which is how a "scalp BTC" instruction quietly becomes a shredder.
    live_spread = broker.spread_bp(args.symbol.upper())   # scalp mode only
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
