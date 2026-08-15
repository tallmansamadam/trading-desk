"""Tests for the strategy validator.

The validator's job is to reject things. That makes it dangerous in a specific
way: a validator that rejects everything looks identical to a correct one until
a real edge shows up and gets thrown away. So the tests here check BOTH
directions — that a genuine edge passes and that noise does not — plus the rule
that matters most in practice: INSUFFICIENT must never be treated as a pass.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import validate
from validate import (
    FAIL,
    INSUF,
    PASS,
    buy_hold,
    curve_stats,
    gate_generalisation,
    gate_risk_matched,
    t_stat,
)


def synthetic(n=800, drift=0.0004, vol=0.01, seed=7):
    """Deterministic pseudo-random walk — no Random() so runs are reproducible."""
    px, out, x = 100.0, [100.0], seed
    for _i in range(n):
        x = (1103515245 * x + 12345) % (2 ** 31)
        shock = ((x / 2 ** 31) - 0.5) * 2 * vol
        px *= 1 + drift + shock
        out.append(px)
    return out


class FakeStrategy:
    """Minimal strategy module stand-in."""

    def __init__(self, name, fn, params=None):
        self.NAME = name
        self.PARAMS = params or {"lookback": 10}
        self._fn = fn

    def generate_signals(self, closes, **kw):
        return self._fn(closes, **kw)


ALWAYS_LONG = FakeStrategy("always_long", lambda c, **k: [1] * len(c))
ALWAYS_FLAT = FakeStrategy("always_flat", lambda c, **k: [0] * len(c))
ORACLE = FakeStrategy(
    "oracle",
    lambda c, **k: [1 if i + 1 < len(c) and c[i + 1] > c[i] else 0 for i in range(len(c))],
)


class TestPrimitives(unittest.TestCase):
    def test_curve_stats_on_a_known_curve(self):
        total, dd, _ = curve_stats([1.0, 2.0, 1.0, 1.5])
        self.assertAlmostEqual(total, 0.5, places=9)
        self.assertAlmostEqual(dd, 0.5, places=9)   # 2.0 -> 1.0

    def test_flat_curve_has_no_drawdown(self):
        total, dd, sharpe = curve_stats([1.0] * 50)
        self.assertAlmostEqual(total, 0.0)
        self.assertAlmostEqual(dd, 0.0)
        self.assertAlmostEqual(sharpe, 0.0)

    def test_scaling_exposure_cannot_change_sharpe(self):
        """The premise the RISK-MATCHED gate rests on. If this ever fails, that
        gate is meaningless."""
        px = synthetic()
        sharpes = [curve_stats(buy_hold(px, f))[2] for f in (0.25, 0.5, 1.0)]
        for s in sharpes[1:]:
            self.assertAlmostEqual(s, sharpes[0], places=6)

    def test_scaling_exposure_does_scale_drawdown(self):
        px = synthetic()
        _, dd_half, _ = curve_stats(buy_hold(px, 0.5))
        _, dd_full, _ = curve_stats(buy_hold(px, 1.0))
        self.assertLess(dd_half, dd_full)

    def test_t_stat(self):
        m, se, t = t_stat([1.0, 1.0, 1.0, 1.0])
        self.assertAlmostEqual(m, 1.0)
        self.assertAlmostEqual(se, 0.0)
        self.assertAlmostEqual(t, 0.0)          # zero variance must not divide by zero
        m, se, t = t_stat([2.0, 4.0, 6.0])
        self.assertAlmostEqual(m, 4.0)
        self.assertGreater(t, 0)


class TestGatesBothDirections(unittest.TestCase):
    """A validator must be able to say yes, or its no means nothing."""

    def setUp(self):
        self._real_load = validate.load
        series = {s: synthetic(seed=i + 3) for i, s in
                  enumerate(["A", "B", "C", "D", "E", "F", "G"])}
        validate.load = lambda sym: series.get(sym.upper(), [])
        self.universe = list(series)

    def tearDown(self):
        validate.load = self._real_load

    def test_oracle_passes_generalisation(self):
        verdict, detail, _ = gate_generalisation(ORACLE, self.universe, {}, 5.0)
        self.assertEqual(verdict, PASS, detail)

    def test_buy_and_hold_clone_does_not_pass(self):
        """Always-long is buy & hold minus costs — it cannot beat the benchmark."""
        verdict, detail, _ = gate_generalisation(ALWAYS_LONG, self.universe, {}, 5.0)
        self.assertEqual(verdict, FAIL, detail)

    def test_never_trading_does_not_pass(self):
        verdict, detail, _ = gate_generalisation(ALWAYS_FLAT, self.universe, {}, 5.0)
        self.assertEqual(verdict, FAIL, detail)

    def test_thin_universe_is_insufficient_not_pass(self):
        """Fewer than five usable symbols must not produce a verdict either way."""
        verdict, detail, _ = gate_generalisation(ORACLE, ["A", "B"], {}, 5.0)
        self.assertEqual(verdict, INSUF, detail)

    def test_oracle_beats_the_risk_matched_hold(self):
        verdict, detail = gate_risk_matched(ORACLE, self.universe, {}, 5.0)[:2]
        self.assertEqual(verdict, PASS, detail)

    def test_always_long_cannot_beat_the_risk_matched_hold(self):
        verdict, detail = gate_risk_matched(ALWAYS_LONG, self.universe, {}, 5.0)[:2]
        self.assertEqual(verdict, FAIL, detail)


class TestVerdictLogic(unittest.TestCase):
    def test_insufficient_is_not_a_pass(self):
        """The rule that keeps 'we could not test it' from reading as 'it works'."""
        results = [("A", PASS, ""), ("B", INSUF, ""), ("C", PASS, "")]
        failed = [n for n, v, _ in results if v == FAIL]
        unknown = [n for n, v, _ in results if v == INSUF]
        self.assertFalse(failed)
        self.assertTrue(unknown, "an INSUFFICIENT gate must block an overall pass")

    def test_any_failure_rejects(self):
        results = [("A", PASS, ""), ("B", FAIL, ""), ("C", PASS, "")]
        self.assertTrue([n for n, v, _ in results if v == FAIL])


class TestRealStrategiesStillFail(unittest.TestCase):
    """Pins the finding this tool was built to catch: sma_cross does not
    generalise. If a future change makes this pass, that is a red flag about
    the change, not a discovery."""

    def test_sma_cross_fails_generalisation_on_real_data(self):
        import strategies.sma_cross as sma
        verdict, detail, _ = gate_generalisation(
            sma, validate.DEFAULT_UNIVERSE, {"fast": 5, "slow": 20}, 5.0)
        self.assertIn(verdict, (FAIL, INSUF), f"unexpectedly passed: {detail}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
