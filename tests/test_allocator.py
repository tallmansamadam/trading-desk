"""Tests for the equal-weight allocator in run_desk.py.

The allocator has to satisfy three caps at once — per order, per symbol, and
book-wide — and the first pass got that wrong: it sized to the position cap and
produced orders that every single risk check rejected. These pin the sizing and
the slicing so that cannot come back.
"""

from __future__ import annotations

import sys
import types
import unittest
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import run_desk
from trading.config import Settings


@dataclass
class C:
    symbol: str


@dataclass
class Item:
    contract: C
    position: float
    marketValue: float
    averageCost: float = 0.0
    marketPrice: float = 0.0
    unrealizedPNL: float = 0.0
    realizedPNL: float = 0.0


class FakeBroker:
    def __init__(self, equity=100_000.0, holdings=None, prices=None, open_orders=0):
        self._equity = equity
        self._holdings = holdings or {}
        self._prices = prices or {}
        self._open = open_orders
        self.sent: list[tuple] = []

    def connect_and_verify(self):
        return "PAFAKE"

    def account(self, refresh=False):
        return {"equity": str(self._equity), "last_equity": str(self._equity),
                "cash": "0", "buying_power": "0"}

    def portfolio(self, account=""):
        return [Item(C(s), 0.0, v) for s, v in self._holdings.items()]

    def positions(self, account=""):
        return []

    def reqAllOpenOrders(self):
        # Order-shaped, because the allocator reads .contract.symbol off these
        # to work out what is already in flight.
        return [types.SimpleNamespace(contract=C(f"OPEN{i}"))
                for i in range(self._open)]

    def reqPnL(self, account="", modelCode=""):
        return types.SimpleNamespace(dailyPnL=0.0, unrealizedPnL=0.0, realizedPnL=0.0)

    def cancelPnL(self, account="", modelCode=""):
        pass

    def sleep(self, _s):
        pass

    def is_crypto(self, s):
        return "/" in s

    def reference_price(self, symbol):
        return self._prices.get(symbol.upper(), 100.0)

    def place_order(self, symbol, side, qty, limit_price=None, tif="day"):
        self.sent.append((symbol, side, qty, limit_price))
        return types.SimpleNamespace(
            contract=C(symbol),
            order=types.SimpleNamespace(orderId="fake1234"),
            orderStatus=types.SimpleNamespace(status="accepted", filled=0,
                                              remaining=qty, avgFillPrice=0.0))


SETTINGS = Settings(mode="paper", max_order_notional=5_000,
                    max_position_notional=10_000, max_gross_notional=70_000,
                    max_open_orders=10, max_daily_loss=500)


def make_args(symbols, **kw):
    base = {"symbols": symbols, "invested": 100.0, "rebalance_days": 21,
            "drift_pct": 25.0, "min_trade": 200.0, "once": True, "arm": False,
            "interval": 1.0}
    base.update(kw)
    return types.SimpleNamespace(**base)


def build(broker, symbols, tmpdir, **kw):
    run_desk.LOG_DIR = Path(tmpdir)
    run_desk.Allocator.STATE = Path(tmpdir) / "state.json"
    return run_desk.Allocator(broker, SETTINGS, make_args(symbols, **kw))


class TestSizing(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmp = tempfile.mkdtemp()

    def test_the_tighter_of_the_two_caps_wins(self):
        """With only 4 names the PER-SYMBOL cap binds before the book cap:
        70k/4 would be 17.5k each, but no name may exceed 10k, so the book can
        only reach 40k. Reaching the 70k gross cap needs at least 7 holdings."""
        a = build(FakeBroker(equity=100_000), ["A", "B", "C", "D"], self.tmp)
        gross, per = a.budget()
        self.assertAlmostEqual(per, 10_000, places=2)
        self.assertAlmostEqual(gross, 40_000, places=2)

    def test_seven_names_is_where_the_book_cap_starts_binding(self):
        seven = build(FakeBroker(equity=100_000), list("ABCDEFG"), self.tmp)
        gross, per = seven.budget()
        self.assertAlmostEqual(per, 10_000, places=2)
        self.assertAlmostEqual(gross, 70_000, places=2)

    def test_per_symbol_is_capped_by_position_limit(self):
        """Four names would be $17.5k each; the per-symbol cap is $10k."""
        a = build(FakeBroker(equity=100_000), ["A", "B", "C", "D"], self.tmp)
        a.budget()
        a2 = build(FakeBroker(equity=100_000), ["A", "B", "C", "D", "E", "F", "G",
                                                "H", "I", "J", "K", "L"], self.tmp)
        _, per2 = a2.budget()
        self.assertLessEqual(per2, SETTINGS.max_position_notional)
        self.assertAlmostEqual(per2, 70_000 / 12, places=2)

    def test_invested_percentage_scales_the_book(self):
        """50% of 100k across 2 names = 25k each, still clipped to the 10k cap."""
        a = build(FakeBroker(equity=100_000), ["A", "B"], self.tmp, invested=50.0)
        gross, per = a.budget()
        self.assertAlmostEqual(per, 10_000, places=2)
        self.assertAlmostEqual(gross, 20_000, places=2)

    def test_a_small_account_is_limited_by_equity_not_the_caps(self):
        a = build(FakeBroker(equity=12_000), list("ABCD"), self.tmp)
        gross, per = a.budget()
        self.assertAlmostEqual(gross, 12_000, places=2)
        self.assertAlmostEqual(per, 3_000, places=2)


class TestSlicing(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmp = tempfile.mkdtemp()

    def test_no_order_exceeds_the_order_cap(self):
        """The bug that made the first version reject every single order."""
        syms = ["A", "B", "C", "D", "E", "F", "G", "H", "I", "J", "K", "L"]
        broker = FakeBroker(equity=100_000, prices=dict.fromkeys(syms, 100.0))
        a = build(broker, syms, self.tmp, arm=True)
        a.rebalance()
        self.assertTrue(broker.sent, "expected orders")
        for sym, _side, qty, _limit in broker.sent:
            notional = qty * 100.0
            self.assertLessEqual(notional, SETTINGS.max_order_notional,
                                 f"{sym} order of ${notional:,.0f} exceeds the cap")

    def test_open_order_cap_throttles_the_pass(self):
        syms = [chr(65 + i) for i in range(12)]
        broker = FakeBroker(equity=100_000, prices=dict.fromkeys(syms, 100.0),
                            open_orders=8)
        a = build(broker, syms, self.tmp, arm=True)
        a.rebalance()
        self.assertLessEqual(len(broker.sent), 2)   # 10 cap - 8 open - 1 buffer

    def test_partial_pass_does_not_bank_the_rebalance_date(self):
        """Banking it early would strand the remainder until the next cadence."""
        syms = [chr(65 + i) for i in range(12)]
        broker = FakeBroker(equity=100_000, prices=dict.fromkeys(syms, 100.0))
        a = build(broker, syms, self.tmp, arm=True)
        a.rebalance()
        self.assertIsNone(a.state.get("last_rebalance"),
                          "a sliced pass must leave the book due for more work")

    def test_tiny_adjustments_are_skipped(self):
        syms = ["A", "B", "C", "D", "E", "F", "G"]
        per = 70_000 / 7
        broker = FakeBroker(equity=100_000, prices=dict.fromkeys(syms, 100.0),
                            holdings=dict.fromkeys(syms, per))
        a = build(broker, syms, self.tmp, arm=True)
        a.rebalance()
        self.assertEqual(broker.sent, [], "an in-balance book should not trade")


class TestCadence(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmp = tempfile.mkdtemp()
        self.syms = ["A", "B", "C", "D"]
        self.broker = FakeBroker(equity=100_000,
                                 prices=dict.fromkeys(self.syms, 100.0))

    def test_first_run_is_always_due(self):
        a = build(self.broker, self.syms, self.tmp)
        due, why = a.due()
        self.assertTrue(due)
        self.assertIn("no prior rebalance", why)

    def test_not_due_inside_the_window(self):
        a = build(self.broker, self.syms, self.tmp)
        a.state["last_rebalance"] = date.today().isoformat()
        due, _ = a.due()
        self.assertFalse(due)

    def test_due_once_the_cadence_elapses(self):
        a = build(self.broker, self.syms, self.tmp)
        a.state["last_rebalance"] = (date.today() - timedelta(days=40)).isoformat()
        due, why = a.due()
        self.assertTrue(due)
        self.assertIn("days since", why)

    def test_drift_triggers_early(self):
        """One holding way off target should not wait for the calendar."""
        broker = FakeBroker(equity=100_000, prices=dict.fromkeys(self.syms, 100.0),
                            holdings={"A": 1.0, "B": 17_500, "C": 17_500, "D": 17_500})
        a = build(broker, self.syms, self.tmp)
        a.state["last_rebalance"] = (date.today() - timedelta(days=2)).isoformat()
        due, why = a.due()
        self.assertTrue(due)
        self.assertIn("drifted", why)

    def test_shadow_mode_never_writes_state(self):
        a = build(self.broker, self.syms, self.tmp, arm=False)
        a.state["last_rebalance"] = date.today().isoformat()
        a._save_state()
        self.assertFalse(a.STATE.exists(),
                         "a shadow run must not record progress it did not make")


if __name__ == "__main__":
    unittest.main(verbosity=2)
