"""Correctness tests for the backtest engine.

The engine is what decides whether real money gets committed to a strategy, so
it needs to be provably free of lookahead bias and provably correct on cases
where the right answer can be computed by hand.

Run: python -m unittest discover tests
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backtest import run_backtest


class TestExecutionLag(unittest.TestCase):
    """The contract: a signal computed on bar i's close is acted on for bar i+1.
    Equivalently, bar i's return is earned with signals[i-1]."""

    def test_signal_captures_the_very_next_bar(self):
        # +100% on bar 1, then -50% on bar 2. signals[0]=1 means "long for bar 1".
        result = run_backtest([100.0, 200.0, 100.0, 100.0], [1, 0, 0, 0], cost_bps=0.0)
        self.assertAlmostEqual(result["total_return"], 1.0, places=9,
                               msg="signal must be acted on the very next bar (1-bar lag)")

    def test_two_bar_lag_would_be_caught(self):
        """Guards the regression directly: an extra bar of lag holds through the
        -50% bar instead of the +100% bar, giving -50%."""
        result = run_backtest([100.0, 200.0, 100.0, 100.0], [1, 0, 0, 0], cost_bps=0.0)
        self.assertNotAlmostEqual(result["total_return"], -0.5, places=6)

    def test_last_bar_signal_has_no_effect(self):
        """No bar follows the final one, so the final signal cannot be traded."""
        flat = run_backtest([100.0, 110.0, 120.0], [0, 0, 0], cost_bps=0.0)
        signalled = run_backtest([100.0, 110.0, 120.0], [0, 0, 1], cost_bps=0.0)
        self.assertAlmostEqual(flat["total_return"], signalled["total_return"], places=9)


class TestNoLookahead(unittest.TestCase):
    def test_perfect_foresight_is_detectable(self):
        """Sensitivity check: if the engine DID leak future data, this would show
        it. A signal that knows the next bar's direction should print a huge
        number — proving the test can detect leakage at all."""
        closes = [100.0]
        for i in range(200):
            closes.append(closes[-1] * (1.05 if i % 2 == 0 else 0.97))
        # signals[i] knows bar i+1's move — legitimate use of the 1-bar contract.
        signals = [1 if (i + 1) < len(closes) and closes[i + 1] > closes[i] else 0
                   for i in range(len(closes))]
        result = run_backtest(closes, signals, cost_bps=0.0)
        self.assertGreater(result["total_return"], 100.0)

    def test_real_strategy_cannot_see_the_future(self):
        """A causal strategy on a series whose future is random must not produce
        implausible returns."""
        import random
        random.seed(42)
        closes = [100.0]
        for _ in range(2000):
            closes.append(closes[-1] * (1 + random.gauss(0, 0.01)))
        # Causal momentum: long if the last bar was up.
        signals = [0] + [1 if closes[i] > closes[i - 1] else 0 for i in range(1, len(closes))]
        result = run_backtest(closes, signals, cost_bps=0.0)
        self.assertLess(abs(result["total_return"]), 5.0,
                        "a causal rule on random data should not 10x")


class TestKnownAnswers(unittest.TestCase):
    def test_always_long_equals_buy_and_hold_minus_one_cost(self):
        closes = [100.0, 105.0, 103.0, 120.0]
        result = run_backtest(closes, [1, 1, 1, 1], cost_bps=10.0)
        expected = (closes[-1] / closes[0]) * (1 - 10.0 / 10_000) - 1
        self.assertAlmostEqual(result["total_return"], expected, places=9)
        self.assertAlmostEqual(result["buy_hold_return"], closes[-1] / closes[0] - 1, places=9)

    def test_always_flat_returns_nothing(self):
        result = run_backtest([100.0, 130.0, 90.0, 110.0], [0, 0, 0, 0], cost_bps=5.0)
        self.assertAlmostEqual(result["total_return"], 0.0, places=9)
        self.assertEqual(result["round_trips"], 0)
        self.assertAlmostEqual(result["max_drawdown"], 0.0, places=9)

    def test_costs_reduce_returns(self):
        closes = [100.0, 110.0, 100.0, 110.0, 100.0, 110.0]
        signals = [1, 0, 1, 0, 1, 0]
        free = run_backtest(closes, signals, cost_bps=0.0)
        pricey = run_backtest(closes, signals, cost_bps=50.0)
        self.assertGreater(free["total_return"], pricey["total_return"])

    def test_round_trip_counted_only_when_closed(self):
        # Enter and never exit -> no completed round trip.
        opened = run_backtest([100.0, 110.0, 120.0], [1, 1, 1], cost_bps=0.0)
        self.assertEqual(opened["round_trips"], 0)
        # Enter then exit -> exactly one.
        closed = run_backtest([100.0, 110.0, 120.0, 130.0], [1, 1, 0, 0], cost_bps=0.0)
        self.assertEqual(closed["round_trips"], 1)

    def test_drawdown_is_measured(self):
        # Long throughout a 100 -> 200 -> 100 round trip: 50% drawdown.
        result = run_backtest([100.0, 200.0, 100.0], [1, 1, 1], cost_bps=0.0)
        self.assertAlmostEqual(result["max_drawdown"], 0.5, places=9)

    def test_mismatched_signal_length_is_rejected(self):
        with self.assertRaises(SystemExit):
            run_backtest([100.0, 101.0, 102.0], [1, 0], cost_bps=0.0)


class TestStrategyContract(unittest.TestCase):
    def test_sma_cross_obeys_the_contract(self):
        from strategies import sma_cross
        closes = [100.0 + i for i in range(100)]
        signals = sma_cross.generate_signals(closes, **sma_cross.PARAMS)
        self.assertEqual(len(signals), len(closes))
        self.assertTrue(set(signals) <= {0, 1})
        # Warmup period must be flat, not long.
        self.assertEqual(signals[: sma_cross.PARAMS["slow"] - 1], [0] * (sma_cross.PARAMS["slow"] - 1))

    def test_sma_cross_is_pure(self):
        from strategies import sma_cross
        closes = [100.0, 102.0, 101.0] * 40
        first = sma_cross.generate_signals(list(closes), fast=5, slow=20)
        second = sma_cross.generate_signals(list(closes), fast=5, slow=20)
        self.assertEqual(first, second)

    def test_sma_cross_goes_long_in_an_uptrend(self):
        from strategies import sma_cross
        closes = [100.0 + i * 2 for i in range(100)]
        signals = sma_cross.generate_signals(closes, fast=10, slow=30)
        self.assertEqual(signals[-1], 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
