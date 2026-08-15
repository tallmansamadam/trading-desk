"""Tests for the Alpaca adapter.

The architectural claim being tested: trading/risk.py is broker-agnostic. The
same engine, the same limits and the same kill switch must govern Alpaca exactly
as they govern IBKR, with no change to risk.py. If that claim breaks, swapping
brokers would silently mean swapping safety.

No network: the HTTP layer is stubbed with canned Alpaca payloads.
"""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from trading.brokers.alpaca import LIVE_BASE, PAPER_BASE, AlpacaBroker, AlpacaError
from trading.config import HALT_FILE, Settings
from trading.market_data import reference_price
from trading.risk import check_order


class FakeHTTP:
    """Routes by URL fragment; records calls so we can assert what was sent."""

    def __init__(self, responses: dict):
        self.responses = responses
        self.calls: list[tuple] = []

    def __call__(self, method, url, body=None):
        self.calls.append((method, url, body))
        for fragment, payload in self.responses.items():
            if fragment in url:
                return payload
        return {}


ACCOUNT = {
    "account_number": "PA123456", "status": "ACTIVE", "currency": "USD",
    "equity": "100000", "last_equity": "100250", "cash": "70000",
    "buying_power": "200000", "long_market_value": "30000",
    "maintenance_margin": "0", "trading_blocked": False,
}

POSITIONS = [
    {"symbol": "AAPL", "qty": "40", "market_value": "8000",
     "avg_entry_price": "195.00", "current_price": "200.00", "unrealized_pl": "200"},
    {"symbol": "MSFT", "qty": "50", "market_value": "22000",
     "avg_entry_price": "430.00", "current_price": "440.00", "unrealized_pl": "500"},
]


def make_broker(mode="paper", responses=None, **extra):
    os.environ["ALPACA_API_KEY"] = "test-key"
    os.environ["ALPACA_SECRET_KEY"] = "test-secret"
    broker = AlpacaBroker(mode=mode)
    payloads = {"/v2/account": ACCOUNT, "/v2/positions": POSITIONS, "/v2/orders": []}
    payloads.update(responses or {})
    payloads.update(extra)
    broker._request = FakeHTTP(payloads)
    return broker


def settings(**kw):
    base = {"mode": "paper", "max_order_notional": 5_000,
                "max_position_notional": 10_000, "max_open_orders": 10, "max_daily_loss": 500}
    if hasattr(Settings(), "max_gross_notional"):
        base["max_gross_notional"] = 70_000
    base.update(kw)
    return Settings(**base)


def named(result, check):
    return next(p for n, p, _ in result.checks if n == check)


class TestCredentialsAndEndpoint(unittest.TestCase):
    def test_missing_credentials_gives_a_clean_error(self):
        for key in ("ALPACA_API_KEY", "ALPACA_SECRET_KEY"):
            os.environ.pop(key, None)
        with self.assertRaises(AlpacaError) as ctx:
            AlpacaBroker()
        self.assertIn("ALPACA_API_KEY", str(ctx.exception))

    def test_paper_mode_uses_the_paper_endpoint(self):
        self.assertEqual(make_broker("paper").base, PAPER_BASE)

    def test_live_mode_uses_the_live_endpoint(self):
        self.assertEqual(make_broker("live").base, LIVE_BASE)

    def test_endpoint_is_never_taken_from_the_environment(self):
        """The base URL must derive only from TRADING_MODE — otherwise a stray
        env var could point 'paper' at the live venue."""
        os.environ["ALPACA_BASE_URL"] = "https://api.alpaca.markets"
        try:
            self.assertEqual(make_broker("paper").base, PAPER_BASE)
        finally:
            os.environ.pop("ALPACA_BASE_URL", None)

    def test_blocked_account_is_refused(self):
        broker = make_broker(responses={"/v2/account": {**ACCOUNT, "trading_blocked": True}})
        with self.assertRaises(AlpacaError):
            broker.connect_and_verify()

    def test_inactive_account_is_refused(self):
        broker = make_broker(responses={"/v2/account": {**ACCOUNT, "status": "SUSPENDED"}})
        with self.assertRaises(AlpacaError):
            broker.connect_and_verify()


class TestInterfaceShape(unittest.TestCase):
    """risk.py and portfolio.py duck-type these; the shapes must match ib_async."""

    def test_positions_shape(self):
        pos = make_broker().positions()
        self.assertEqual(pos[0].contract.symbol, "AAPL")
        self.assertEqual(pos[0].position, 40.0)

    def test_portfolio_carries_market_value(self):
        items = make_broker().portfolio()
        self.assertEqual({i.contract.symbol: i.marketValue for i in items},
                         {"AAPL": 8000.0, "MSFT": 22000.0})

    def test_daily_pnl_is_equity_minus_last_equity(self):
        # 100000 - 100250 = -250
        self.assertAlmostEqual(make_broker().reqPnL().dailyPnL, -250.0)

    def test_open_orders_shape(self):
        order = [{"id": "abcdef123456", "symbol": "SPY", "side": "buy", "qty": "3",
                  "type": "limit", "limit_price": "400", "status": "new",
                  "filled_qty": "0", "time_in_force": "day"}]
        trades = make_broker(responses={"/v2/orders": order}).reqAllOpenOrders()
        self.assertEqual(trades[0].order.action, "BUY")
        self.assertEqual(trades[0].order.orderType, "LMT")
        self.assertEqual(trades[0].orderStatus.remaining, 3.0)

    def test_account_summary_maps_to_ibkr_tags(self):
        tags = {v.tag for v in make_broker().accountSummary()}
        self.assertIn("NetLiquidation", tags)
        self.assertIn("BuyingPower", tags)


class TestSharedRiskEngine(unittest.TestCase):
    """The whole point: the SAME risk.py governs Alpaca."""

    def setUp(self):
        if HALT_FILE.exists():
            HALT_FILE.unlink()

    def tearDown(self):
        if HALT_FILE.exists():
            HALT_FILE.unlink()

    def test_normal_order_approved(self):
        result = check_order(make_broker(), "PA1", settings(), "SPY", "BUY", 5,
                             limit_price=100.0)
        self.assertTrue(result.approved, result.report())

    def test_order_notional_limit_enforced(self):
        result = check_order(make_broker(), "PA1", settings(), "SPY", "BUY", 100,
                             limit_price=100.0)
        self.assertFalse(named(result, "order-notional"))

    def test_position_notional_uses_alpaca_positions(self):
        """AAPL already 40 sh; buying 25 more at $200 = $13k, over the $10k cap."""
        result = check_order(make_broker(), "PA1", settings(), "AAPL", "BUY", 25,
                             limit_price=200.0)
        self.assertFalse(named(result, "position-notional"))

    def test_restricted_list_applies_to_alpaca_too(self):
        result = check_order(make_broker(), "PA1", settings(restricted_symbols=("GME",)),
                             "GME", "BUY", 1, limit_price=20.0)
        self.assertFalse(named(result, "restricted-list"))

    def test_halt_file_blocks_alpaca_orders(self):
        HALT_FILE.write_text("test halt\n")
        result = check_order(make_broker(), "PA1", settings(), "SPY", "BUY", 1,
                             limit_price=100.0)
        self.assertFalse(named(result, "kill-switch"))

    def test_daily_loss_limit_reads_alpaca_equity(self):
        """equity 100000 vs last_equity 100600 = -600, past the -500 limit."""
        broker = make_broker(responses={"/v2/account": {**ACCOUNT, "last_equity": "100600"}})
        result = check_order(broker, "PA1", settings(), "SPY", "BUY", 1, limit_price=100.0)
        self.assertFalse(named(result, "daily-loss"))

    @unittest.skipUnless(hasattr(Settings(), "max_gross_notional"),
                         "gross-exposure cap not applied")
    def test_gross_exposure_uses_alpaca_market_values(self):
        """Book is already $30k; a $45k order would take gross past $70k."""
        result = check_order(make_broker(), "PA1", settings(), "SPY", "BUY", 450,
                             limit_price=100.0)
        self.assertFalse(named(result, "gross-exposure"))

    @unittest.skipUnless(hasattr(Settings(), "max_gross_notional"),
                         "gross-exposure cap not applied")
    def test_de_risking_sell_allowed_when_over_gross(self):
        result = check_order(make_broker(), "PA1", settings(max_gross_notional=10_000),
                             "MSFT", "SELL", 25, limit_price=440.0)
        self.assertTrue(named(result, "gross-exposure"))


class TestPriceDispatch(unittest.TestCase):
    """market_data.reference_price must route to the broker's own pricing,
    which is what keeps risk.py broker-agnostic without editing it."""

    def test_dispatches_to_the_broker(self):
        broker = make_broker(responses={
            "/trades/latest": {"trade": {"p": 123.45}},
        })
        self.assertAlmostEqual(reference_price(broker, "SPY"), 123.45)

    def test_falls_back_to_quote_mid(self):
        broker = make_broker(responses={
            "/trades/latest": {"trade": {"p": 0}},
            "/quotes/latest": {"quote": {"bp": 100.0, "ap": 102.0}},
        })
        self.assertAlmostEqual(reference_price(broker, "SPY"), 101.0)

    def test_falls_back_to_last_bar_when_market_is_shut(self):
        broker = make_broker(responses={
            "/trades/latest": {"trade": {"p": 0}},
            "/quotes/latest": {"quote": {"bp": 0, "ap": 0}},
            "/bars": {"bars": [{"t": "2026-08-14T00:00:00Z", "c": 776.34}]},
        })
        self.assertAlmostEqual(reference_price(broker, "SPY"), 776.34)


if __name__ == "__main__":
    unittest.main(verbosity=2)
