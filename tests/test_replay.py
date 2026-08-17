"""Tests for the replay broker.

The replay harness is only worth having if its fill model is honest. A broker
that fills too easily turns a replay into a fantasy, and one that never fills
makes the desk look broken. These pin the model, and — more importantly — pin
that the replay cannot see the future.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from trading.brokers.replay import ReplayBroker


def series(*closes, spread=1.0):
    """Bars whose high/low straddle the close by `spread`."""
    return [{"o": c, "h": c + spread, "l": c - spread, "c": c, "v": 1000} for c in closes]


def broker(closes=(100.0, 101.0, 102.0), **kw):
    dates = [f"2026-01-{i+1:02d}" for i in range(len(closes))]
    return ReplayBroker({"AAA": series(*closes)}, dates, **kw)


class TestFillModel(unittest.TestCase):
    def test_a_marketable_buy_limit_fills(self):
        b = broker((100.0, 100.0))
        b.place_order("AAA", "BUY", 10, 100.5)     # above the low, so it trades
        b.advance()
        self.assertEqual(len(b.fills), 1)
        self.assertEqual(b.shares["AAA"], 10)

    def test_a_buy_limit_below_the_low_does_not_fill(self):
        b = broker((100.0, 100.0))
        b.place_order("AAA", "BUY", 10, 90.0)      # tape never trades there
        b.advance()
        self.assertEqual(b.fills, [])

    def test_a_gap_through_the_limit_fills_at_the_better_price(self):
        """Opening below a buy limit should fill at the open, not the limit —
        anything else silently flatters or penalises the strategy."""
        b = ReplayBroker({"AAA": series(100.0) + [{"o": 90.0, "h": 91.0, "l": 89.0,
                                                   "c": 90.0, "v": 1}]},
                         ["2026-01-01", "2026-01-02"])
        b.place_order("AAA", "BUY", 10, 99.0)
        b.advance()
        self.assertAlmostEqual(b.fills[0].price, 90.0)

    def test_sell_limit_needs_the_tape_to_reach_it(self):
        b = broker((100.0, 100.0))
        b.shares["AAA"] = 10
        b.place_order("AAA", "SELL", 10, 105.0)
        b.advance()
        self.assertEqual(b.fills, [], "the high never reached 105")

    def test_market_orders_fill_at_the_next_open(self):
        b = ReplayBroker({"AAA": series(100.0) + [{"o": 97.5, "h": 99.0, "l": 96.0,
                                                   "c": 98.0, "v": 1}]},
                         ["2026-01-01", "2026-01-02"])
        b.place_order("AAA", "BUY", 5, None)
        b.advance()
        self.assertAlmostEqual(b.fills[0].price, 97.5)

    def test_day_orders_get_one_session_then_die(self):
        """Mirrors the live behaviour: an order placed outside a session queues
        for the NEXT one, gets that session to fill, and expires at its close."""
        b = broker((100.0, 100.0, 100.0))
        b.place_order("AAA", "BUY", 10, 50.0, "day")   # unreachable limit
        self.assertEqual(len(b.resting), 1, "rests until the next session opens")
        b.advance()                                    # its one session
        self.assertEqual(b.resting, [], "must not survive that session's close")
        self.assertEqual(b.fills, [], "and it never traded at 50")

    def test_costs_reduce_cash(self):
        free = broker((100.0, 100.0), cost_bps=0.0)
        paid = broker((100.0, 100.0), cost_bps=50.0)
        for b in (free, paid):
            b.place_order("AAA", "BUY", 10, 101.0)
            b.advance()
        self.assertLess(paid.cash, free.cash)


class TestNoLookahead(unittest.TestCase):
    def test_bars_stop_at_the_present(self):
        """The whole harness is worthless if the strategy can see forward."""
        b = broker((100.0, 200.0, 300.0))
        seen = [x["c"] for x in b.historical_bars("AAA", "1Day", 50)]
        self.assertEqual(seen, [100.0], "bar 0 must not see bars 1 or 2")
        b.advance()
        seen = [x["c"] for x in b.historical_bars("AAA", "1Day", 50)]
        self.assertEqual(seen, [100.0, 200.0])

    def test_reference_price_is_the_current_bar(self):
        b = broker((100.0, 200.0))
        self.assertAlmostEqual(b.reference_price("AAA"), 100.0)
        b.advance()
        self.assertAlmostEqual(b.reference_price("AAA"), 200.0)


class TestAccounting(unittest.TestCase):
    def test_equity_tracks_the_mark(self):
        b = broker((100.0, 100.0, 150.0), equity=10_000.0)
        b.place_order("AAA", "BUY", 10, 101.0)
        b.advance()                                  # fills near 100
        start = b.equity()
        b.advance()                                  # marks to 150
        self.assertGreater(b.equity(), start)
        self.assertAlmostEqual(b.equity(), b.cash + 10 * 150.0, places=6)

    def test_daily_pnl_resets_each_session(self):
        b = broker((100.0, 100.0, 150.0), equity=10_000.0)
        b.place_order("AAA", "BUY", 10, 101.0)
        b.advance()
        b.advance()
        self.assertGreater(b.reqPnL().dailyPnL, 0)

    def test_round_trip_realises_the_gain(self):
        b = broker((100.0, 100.0, 150.0), equity=10_000.0)
        b.place_order("AAA", "BUY", 10, 101.0)
        b.advance()
        b.place_order("AAA", "SELL", 10, 140.0)
        b.advance()
        self.assertGreater(b.realized, 0)
        self.assertNotIn("AAA", b.shares)


class TestSimulatedClock(unittest.TestCase):
    def test_sim_seconds_advances_with_bars_not_wall_clock(self):
        """The bug the first replay exposed: a wall-clock backoff silenced the
        desk for an entire run, because a decade replays in seconds."""
        b = broker((100.0, 100.0, 100.0))
        t0 = b.sim_seconds()
        b.advance()
        t1 = b.sim_seconds()
        self.assertAlmostEqual(t1 - t0, ReplayBroker.SESSION_MINUTES * 60)
        b.set_minute(30)
        self.assertAlmostEqual(b.sim_seconds() - t1, 30 * 60)

    def test_clock_reports_an_open_market_inside_the_session(self):
        b = broker()
        b.set_minute(60)
        c = b._trading("GET", "/v2/clock")
        self.assertTrue(c["is_open"])
        self.assertIn("next_close", c)

    def test_unknown_endpoints_raise_rather_than_pretend(self):
        """A silent empty response would look like a working call."""
        with self.assertRaises(SystemExit):
            broker()._trading("GET", "/v2/account/portfolio/history")


if __name__ == "__main__":
    unittest.main(verbosity=2)
