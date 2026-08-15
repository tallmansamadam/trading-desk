"""Safety tests for the parts that must never regress: the paper/live account
check, the pre-trade risk limits, and the live-order refusal.

Run: python -m unittest discover tests
No TWS connection needed — IBKR is stubbed.
"""

from __future__ import annotations

import os
import sys
import unittest
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from trading import risk as risk_mod
from trading.config import HALT_FILE, Settings
from trading.connection import ModeMismatchError, _verify_mode
from trading.orders import place_order

# --- stubs -----------------------------------------------------------------

@dataclass
class FakeContract:
    symbol: str


@dataclass
class FakePosition:
    contract: FakeContract
    position: float


@dataclass
class FakePortfolioItem:
    contract: FakeContract
    marketValue: float


@dataclass
class FakePnL:
    dailyPnL: float | None = 0.0
    unrealizedPnL: float = 0.0
    realizedPnL: float = 0.0


class FakeIB:
    """Minimal stand-in for ib_async.IB covering what risk.check_order touches."""

    def __init__(self, positions=(), open_orders=0, daily_pnl=0.0, mark=100.0):
        self._positions = list(positions)
        self._open_orders = open_orders
        self._daily_pnl = daily_pnl
        self._mark = mark

    def positions(self, _account=None):
        return self._positions

    def portfolio(self, _account=None):
        """Used by the gross-exposure check, which marks the book to market."""
        return [
            FakePortfolioItem(p.contract, p.position * self._mark)
            for p in self._positions
        ]

    def reqAllOpenOrders(self):
        return [object()] * self._open_orders

    def reqPnL(self, _account):
        return FakePnL(dailyPnL=self._daily_pnl)

    def cancelPnL(self, _account):
        pass

    def sleep(self, _seconds):
        pass


class ExplodingIB(FakeIB):
    """Any use at all is a test failure — proves refusal happens before I/O."""

    def __getattribute__(self, name):
        raise AssertionError(f"IB.{name} must not be touched on a refused order")


BASE = Settings(
    mode="paper",
    max_order_notional=5_000,
    max_position_notional=10_000,
    max_open_orders=10,
    max_daily_loss=500,
)


def named(result, check):
    """Look up a single check's pass/fail by name."""
    return next(passed for name, passed, _ in result.checks if name == check)


# --- paper / live account verification -------------------------------------

class TestModeVerification(unittest.TestCase):
    def test_paper_account_in_paper_mode_ok(self):
        _verify_mode("DU1234567", BASE)  # must not raise

    def test_live_account_in_live_mode_ok(self):
        _verify_mode("U1234567", Settings(mode="live"))

    def test_live_mode_with_paper_account_refused(self):
        with self.assertRaises(ModeMismatchError):
            _verify_mode("DU1234567", Settings(mode="live"))

    def test_paper_mode_with_live_account_refused(self):
        """The dangerous direction: thinking you're on paper, hitting a live account."""
        with self.assertRaises(ModeMismatchError):
            _verify_mode("U1234567", BASE)


# --- risk engine -----------------------------------------------------------

class TestRiskEngine(unittest.TestCase):
    def setUp(self):
        self._real_price = risk_mod.reference_price
        risk_mod.reference_price = lambda _ib, _sym: 100.0
        if HALT_FILE.exists():
            HALT_FILE.unlink()

    def tearDown(self):
        risk_mod.reference_price = self._real_price
        if HALT_FILE.exists():
            HALT_FILE.unlink()

    def check(self, ib=None, settings=None, symbol="AAPL", side="BUY", qty=10, limit=None):
        return risk_mod.check_order(
            ib or FakeIB(), "DU1", settings or BASE, symbol, side, qty, limit
        )

    def test_normal_order_approved(self):
        self.assertTrue(self.check().approved)  # 10 sh @ $100 = $1,000

    def test_order_notional_limit_enforced(self):
        result = self.check(qty=100)  # $10,000 > $5,000 limit
        self.assertFalse(result.approved)
        self.assertFalse(named(result, "order-notional"))

    def test_position_notional_limit_uses_existing_position(self):
        """40 sh held + 40 sh new = $8,000 order ok, but tests the position path."""
        ib = FakeIB(positions=[FakePosition(FakeContract("AAPL"), 60)])
        result = self.check(ib=ib, qty=45)  # 105 sh = $10,500 > $10,000
        self.assertFalse(result.approved)
        self.assertFalse(named(result, "position-notional"))

    def test_sell_reduces_position_notional(self):
        ib = FakeIB(positions=[FakePosition(FakeContract("AAPL"), 95)])
        result = self.check(ib=ib, side="SELL", qty=45)  # down to 50 sh
        self.assertTrue(named(result, "position-notional"))

    def test_halt_file_blocks_order(self):
        HALT_FILE.write_text("test halt\n")
        result = self.check()
        self.assertFalse(result.approved)
        self.assertFalse(named(result, "kill-switch"))

    def test_restricted_symbol_blocked(self):
        settings = Settings(mode="paper", restricted_symbols=("TSLA",))
        self.assertFalse(self.check(settings=settings, symbol="tsla").approved)

    def test_daily_loss_limit_enforced(self):
        result = self.check(ib=FakeIB(daily_pnl=-600.0))  # limit is -500
        self.assertFalse(result.approved)
        self.assertFalse(named(result, "daily-loss"))

    def test_daily_loss_within_limit_passes(self):
        self.assertTrue(named(self.check(ib=FakeIB(daily_pnl=-400.0)), "daily-loss"))

    def test_open_order_cap_enforced(self):
        result = self.check(ib=FakeIB(open_orders=10))
        self.assertFalse(result.approved)
        self.assertFalse(named(result, "open-orders"))

    def test_zero_and_negative_quantity_rejected(self):
        self.assertFalse(self.check(qty=0).approved)
        self.assertFalse(self.check(qty=-5).approved)

    def test_limit_price_used_instead_of_market_price(self):
        """A $400 limit on 20 sh = $8,000, over the limit, even though ref is $100."""
        self.assertFalse(self.check(qty=20, limit=400.0).approved)

    def test_live_mode_requires_human_ack(self):
        os.environ.pop("LIVE_TRADING_ACK", None)
        result = self.check(settings=Settings(mode="live"))
        self.assertFalse(result.approved)
        self.assertFalse(named(result, "live-ack"))


# --- live order refusal ----------------------------------------------------

class TestLiveOrderRefusal(unittest.TestCase):
    def test_live_order_without_confirm_flag_refused_before_any_ib_call(self):
        with self.assertRaises(SystemExit) as ctx:
            place_order(
                ExplodingIB(), "U1", Settings(mode="live"), "AAPL", "BUY", 1,
                confirm_live=False,
            )
        self.assertIn("--confirm-live", str(ctx.exception))


if __name__ == "__main__":
    unittest.main(verbosity=2)
