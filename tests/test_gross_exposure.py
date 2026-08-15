"""Tests for the portfolio-level gross exposure cap.

These skip until the gross-exposure check is applied to trading/risk.py, so the
suite stays green either way. Once applied they activate automatically.

The cap exists because max_position_notional is PER SYMBOL: without a book-level
ceiling, N symbols can each sit at their cap and no check objects. A 10-year
7-symbol replay reached $104,570 gross on a $100k account that way.
"""

from __future__ import annotations

import sys
import unittest
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from trading.config import HALT_FILE, Settings
from trading.risk import check_order

HAS_GROSS = hasattr(Settings(), "max_gross_notional")
REASON = "gross-exposure cap not yet applied to trading/config.py + trading/risk.py"


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


class FakePnL:
    dailyPnL = 0.0
    unrealizedPnL = 0.0
    realizedPnL = 0.0


class BookIB:
    """Fake broker holding {symbol: (shares, market_value)}."""

    def __init__(self, book: dict[str, tuple[float, float]] | None = None) -> None:
        self.book = book or {}

    def positions(self, _account=""):
        return [FakePosition(FakeContract(s), q) for s, (q, _v) in self.book.items()]

    def portfolio(self, _account=""):
        return [FakePortfolioItem(FakeContract(s), v) for s, (_q, v) in self.book.items()]

    def reqAllOpenOrders(self):
        return []

    def reqPnL(self, _account, _modelCode=""):
        return FakePnL()

    def cancelPnL(self, _account, _modelCode=""):
        pass

    def sleep(self, _seconds):
        pass


def _settings(**kw):
    base = {
        "mode": "paper", "max_order_notional": 5_000, "max_position_notional": 10_000,
        "max_open_orders": 10, "max_daily_loss": 500,
    }
    if HAS_GROSS:
        base["max_gross_notional"] = 30_000
    base.update(kw)
    return Settings(**base)


def gross_passed(result):
    return next(p for n, p, _ in result.checks if n == "gross-exposure")


def gross_detail(result):
    return next(d for n, _p, d in result.checks if n == "gross-exposure")


@unittest.skipUnless(HAS_GROSS, REASON)
class TestGrossExposure(unittest.TestCase):
    def setUp(self):
        if HALT_FILE.exists():
            HALT_FILE.unlink()

    def check(self, book, symbol="SPY", side="BUY", qty=40, price=100.0, **kw):
        return check_order(
            BookIB(book), "DU1", _settings(**kw), symbol, side, qty, limit_price=price
        )

    def test_empty_book_allows_entry(self):
        self.assertTrue(self.check({}).approved)

    def test_blocks_a_fourth_name_once_the_book_is_full(self):
        """3 x $10k = $30k. The 4th entry is exactly what this cap exists to stop."""
        book = {"AAA": (100, 10_000), "BBB": (100, 10_000), "CCC": (100, 10_000)}
        result = self.check(book)
        self.assertFalse(gross_passed(result))
        self.assertFalse(result.approved)

    def test_gross_is_the_only_check_that_catches_it(self):
        """Proves the hole is real: every per-symbol check passes."""
        book = {"AAA": (100, 10_000), "BBB": (100, 10_000), "CCC": (100, 10_000)}
        failed = [n for n, passed, _ in self.check(book).checks if not passed]
        self.assertEqual(failed, ["gross-exposure"])

    def test_de_risking_sell_is_never_blocked(self):
        """An oversized book must always have a way back down."""
        book = {"AAA": (500, 50_000), "SPY": (200, 20_000)}
        result = self.check(book, side="SELL", qty=50)
        self.assertTrue(gross_passed(result), "a reducing order must always pass")
        self.assertIn("reduces exposure", gross_detail(result))

    def test_flatten_from_oversized_book_allowed(self):
        book = {"SPY": (100, 10_000), "AAA": (900, 90_000)}
        self.assertTrue(gross_passed(self.check(book, side="SELL", qty=100)))

    def test_buy_still_blocked_while_over_the_ceiling(self):
        self.assertFalse(gross_passed(self.check({"AAA": (500, 50_000)}, qty=10)))

    def test_adding_to_an_existing_position_is_not_double_counted(self):
        """SPY 40->80 sh adds $4k of gross, not $8k."""
        book = {"SPY": (40, 4_000), "AAA": (100, 10_000)}
        result = self.check(book, qty=40)
        self.assertIn("18,000", gross_detail(result))
        self.assertTrue(gross_passed(result))

    def test_appreciated_position_counts_at_market_value(self):
        """A holding that drifted past its per-symbol cap still counts in gross."""
        book = {"AAPL": (100, 48_929)}  # 4.89x its $10k cap, as the replay found
        result = self.check(book, qty=10)
        self.assertFalse(gross_passed(result))
        self.assertIn("49,929", gross_detail(result))

    def test_cap_is_configurable(self):
        book = {"AAA": (100, 10_000), "BBB": (100, 10_000), "CCC": (100, 10_000)}
        self.assertTrue(gross_passed(self.check(book, max_gross_notional=70_000)))
        self.assertFalse(gross_passed(self.check(book, max_gross_notional=20_000)))


if __name__ == "__main__":
    unittest.main(verbosity=2)
