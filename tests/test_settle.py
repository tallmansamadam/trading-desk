"""Pins the settle behaviour found in production.

The allocator ran a full rebalance pass every 60 seconds for ten minutes
straight against a book it could not act on. Every remaining gap was smaller
than one share of the relevant ETF, so no order was placeable — but the early
return skipped the code that banks the rebalance date, so the next tick found
the book "due" again and replayed the same impossible pass.

Harmless in effect, wasteful in practice, and exactly the kind of loop that
looks like activity while achieving nothing. These tests fail if it returns.
"""

from __future__ import annotations

import sys
import tempfile
import types
import unittest
from dataclasses import dataclass
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


class Broker:
    def __init__(self, holdings=None, prices=None, open_orders=()):
        self._holdings = holdings or {}
        self._prices = prices or {}
        self._open = list(open_orders)
        self.sent = []

    def connect_and_verify(self):
        return "PAFAKE"

    def account(self, refresh=False):
        return {"equity": "100000", "last_equity": "100000"}

    def portfolio(self, account=""):
        return [Item(C(s), 0.0, v) for s, v in self._holdings.items()]

    def positions(self, account=""):
        return []

    def reqAllOpenOrders(self):
        return [types.SimpleNamespace(contract=C(s)) for s in self._open]

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
        self.sent.append((symbol, side, qty))
        return types.SimpleNamespace(
            contract=C(symbol), order=types.SimpleNamespace(orderId="fake"),
            orderStatus=types.SimpleNamespace(status="accepted", filled=0,
                                              remaining=qty, avgFillPrice=0.0))


SETTINGS = Settings(mode="paper", max_order_notional=5_000,
                    max_position_notional=10_000, max_gross_notional=70_000,
                    max_open_orders=10, max_daily_loss=500)

SYMS = list("ABCDEFGHIJKL")           # 12 names -> $5,833 each


def build(broker, **kw):
    tmp = tempfile.mkdtemp()
    run_desk.LOG_DIR = Path(tmp)
    run_desk.Allocator.STATE = Path(tmp) / "state.json"
    base = {"symbols": SYMS, "invested": 100.0, "rebalance_days": 21,
            "drift_pct": 25.0, "min_trade": 200.0, "once": True, "arm": True,
            "interval": 1.0}
    base.update(kw)
    return run_desk.Allocator(broker, SETTINGS, types.SimpleNamespace(**base))


class TestSettles(unittest.TestCase):
    def test_sub_share_gaps_settle_instead_of_looping(self):
        """The production bug: a $406 gap on a $775 share buys zero shares, so
        the pass is unactionable and must be treated as finished."""
        # every name ~$400 short of the $5,833 target, priced at $775
        holdings = dict.fromkeys(SYMS, 5_430.0)
        broker = Broker(holdings=holdings, prices=dict.fromkeys(SYMS, 775.0))
        a = build(broker)
        placed = a.rebalance()
        self.assertEqual(placed, 0, "no whole share fits in the gap")
        self.assertEqual(broker.sent, [])
        self.assertIsNotNone(a.state.get("last_rebalance"),
                             "an unactionable book must bank the date, or the "
                             "service replays the same pass forever")

    def test_a_settled_book_is_not_due_again(self):
        holdings = dict.fromkeys(SYMS, 5_430.0)
        a = build(Broker(holdings=holdings, prices=dict.fromkeys(SYMS, 775.0)))
        a.rebalance()
        due, why = a.due()
        self.assertFalse(due, f"should be settled, got: {why}")

    def test_pending_work_still_blocks_settling(self):
        """An unfilled order IS outstanding work — that must not bank the date,
        or the remainder would be stranded until the next cadence."""
        holdings = dict.fromkeys(SYMS[:11], 5_430.0)
        broker = Broker(holdings=holdings,
                        prices=dict.fromkeys(SYMS, 775.0),
                        open_orders=["L"])
        a = build(broker)
        a.rebalance()
        self.assertIsNone(a.state.get("last_rebalance"),
                          "a resting order means the book is not finished")

    def test_a_real_gap_still_trades(self):
        """The settle path must not swallow genuinely actionable rebalances."""
        holdings = dict.fromkeys(SYMS, 0.0)
        broker = Broker(holdings=holdings, prices=dict.fromkeys(SYMS, 100.0))
        a = build(broker)
        placed = a.rebalance()
        self.assertGreater(placed, 0)
        self.assertTrue(broker.sent)


if __name__ == "__main__":
    unittest.main(verbosity=2)
