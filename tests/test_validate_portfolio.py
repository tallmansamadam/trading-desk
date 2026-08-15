"""Tests for the allocation validator.

The engine here is easy to get subtly wrong in ways that silently invalidate
every result. The one that already bit: weights that do not drift with prices
mean the book is implicitly rebalanced every bar, turnover reads near zero, and
the rebalance cadence changes nothing — so a rebalancing study measures nothing.
That behaviour is pinned below.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import portfolios.equal_weight as ew
import validate_portfolio as vp
from validate_portfolio import (
    FAIL,
    INSUF,
    gate_beats_equal_weight,
    gate_not_one_asset,
    run_portfolio,
)


def ramp(n, rate):
    px, out = 100.0, [100.0]
    for _ in range(n):
        px *= 1 + rate
        out.append(px)
    return out


def wobble(n, amp, seed=1):
    """Deterministic alternating series — no RNG, so results are reproducible."""
    px, out, x = 100.0, [100.0], seed
    for _i in range(n):
        x = (1103515245 * x + 12345) % (2 ** 31)
        px *= 1 + amp * (1 if (x >> 16) % 2 else -1)
        out.append(px)
    return out


class TestEngine(unittest.TestCase):
    def test_single_asset_full_weight_reproduces_the_asset(self):
        px = ramp(200, 0.001)
        closes = {"A": px}
        w = [{"A": 1.0}] + [{}] * (len(px) - 1)
        eq = run_portfolio(closes, w, 0.0)["equity"]
        self.assertAlmostEqual(eq[-1], px[-1] / px[0], places=6)

    def test_weights_drift_between_rebalances(self):
        """The bug this suite exists for. Two assets, one rising and one flat:
        after a single allocation the riser MUST become the larger weight."""
        n = 100
        closes = {"UP": ramp(n, 0.01), "FLAT": ramp(n, 0.0)}
        w = [{"UP": 0.5, "FLAT": 0.5}] + [{}] * n
        eq = run_portfolio(closes, w, 0.0)["equity"]
        # buy and hold of the pair: half doubles-ish, half stays put
        expected = 0.5 * (closes["UP"][-1] / closes["UP"][0]) + 0.5
        self.assertAlmostEqual(eq[-1], expected, places=4)

    def test_no_drift_would_give_a_different_answer(self):
        """Guards against a regression that silently restores continuous
        rebalancing: constant-weight rebalancing beats buy-and-hold here."""
        n = 100
        closes = {"UP": ramp(n, 0.01), "FLAT": ramp(n, 0.0)}
        drifting = run_portfolio(closes, [{"UP": .5, "FLAT": .5}] + [{}] * n, 0.0)
        rebalanced = run_portfolio(closes, [{"UP": .5, "FLAT": .5}] * (n + 1), 0.0)
        self.assertNotAlmostEqual(drifting["equity"][-1],
                                  rebalanced["equity"][-1], places=3)

    def test_turnover_rises_as_rebalancing_gets_more_frequent(self):
        closes = {"A": wobble(600, 0.02, 1), "B": wobble(600, 0.02, 9)}
        n = len(closes["A"])
        slow = run_portfolio(closes, ew.generate_weights(closes, n, rebalance_days=63), 5.0)
        fast = run_portfolio(closes, ew.generate_weights(closes, n, rebalance_days=5), 5.0)
        self.assertGreater(fast["turnover_per_year"], slow["turnover_per_year"])
        self.assertGreater(fast["rebalances"], slow["rebalances"])

    def test_cost_reduces_equity(self):
        closes = {"A": wobble(400, 0.02, 3), "B": wobble(400, 0.02, 5)}
        n = len(closes["A"])
        w = ew.generate_weights(closes, n, rebalance_days=5)
        free = run_portfolio(closes, w, 0.0)["equity"][-1]
        pricey = run_portfolio(closes, w, 50.0)["equity"][-1]
        self.assertLess(pricey, free)

    def test_unallocated_weight_is_cash_and_earns_nothing(self):
        px = ramp(100, 0.01)
        half = run_portfolio({"A": px}, [{"A": 0.5}] + [{}] * 100, 0.0)["equity"][-1]
        full = run_portfolio({"A": px}, [{"A": 1.0}] + [{}] * 100, 0.0)["equity"][-1]
        self.assertLess(half, full)
        self.assertGreater(half, 1.0)


class TestGates(unittest.TestCase):
    def setUp(self):
        self.closes = {c: wobble(900, 0.012, i + 1)
                       for i, c in enumerate("ABCDEFGH")}

    def test_equal_weight_cannot_beat_itself(self):
        """The null control. Any non-zero delta here means the benchmark and the
        rule under test are not being run through the same engine."""
        verdict, detail, deltas = gate_beats_equal_weight(
            ew, self.closes, dict(ew.PARAMS), 5.0, subs=6, size=4)
        self.assertEqual(verdict, FAIL, detail)
        for d in deltas:
            self.assertAlmostEqual(d, 0.0, places=9)

    def test_tiny_universe_is_insufficient_not_pass(self):
        small = dict(list(self.closes.items())[:3])
        verdict, detail, _ = gate_beats_equal_weight(
            ew, small, dict(ew.PARAMS), 5.0)
        self.assertEqual(verdict, INSUF, detail)

    def test_jackknife_needs_an_advantage_to_attribute(self):
        verdict, detail = gate_not_one_asset(
            ew, self.closes, dict(ew.PARAMS), 5.0)[:2]
        self.assertEqual(verdict, FAIL, detail)


class TestRealRulesStillFail(unittest.TestCase):
    """Pins the finding: inverse-vol weighting loses to naive 1/N, reproducing
    DeMiguel/Garlappi/Uppal. If this starts passing, suspect the change."""

    def test_risk_parity_does_not_beat_equal_weight(self):
        import portfolios.risk_parity as rp
        _, closes = vp.load_aligned(vp.DEFAULT_UNIVERSE)
        if len(closes) < 8:
            self.skipTest("universe data not present")
        verdict, detail, _ = gate_beats_equal_weight(
            rp, closes, dict(rp.PARAMS), 5.0, subs=8, size=6)
        self.assertIn(verdict, (FAIL, INSUF), f"unexpectedly passed: {detail}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
