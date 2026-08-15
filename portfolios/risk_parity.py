"""Inverse-volatility weighting — equalise risk contribution, not dollars.

Equal weight gives every asset the same money. That hands the volatile assets
most of the portfolio's actual risk: a 40%-vol name and a 6%-vol name at equal
dollars are not equal positions in any sense that matters. Inverse-vol weighting
sets w_i proportional to 1/sigma_i, so each holding contributes comparable risk.

Same underlying premise as strategies/vol_target.py — volatility is forecastable
where direction is not — but applied ACROSS assets rather than through time.
That difference matters: a cross-sectional rule does not need volatility to be
predictable over time, only for the relative ordering of assets to persist,
which is a considerably weaker and more durable claim.

Correlations are deliberately ignored. Estimating a full covariance matrix from
ten years of daily data across twenty-odd assets is exactly the estimation-error
trap that makes optimisers underperform 1/N. Inverse vol needs only a diagonal.
"""

import math

NAME = "risk_parity"
PARAMS = {
    "lookback": 60,          # trading days in the volatility estimate
    "rebalance_days": 21,    # roughly monthly
    "max_weight": 40,        # cap on any single holding, percent
}


def _trailing_vol(series: list[float], end: int, lookback: int) -> float | None:
    lo = end - lookback
    if lo < 1:
        return None
    rets = [series[i] / series[i - 1] - 1 for i in range(lo, end) if series[i - 1]]
    if len(rets) < lookback // 2:
        return None
    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / max(len(rets) - 1, 1)
    return math.sqrt(var) or None


def generate_weights(closes: dict[str, list[float]], n_bars: int,
                     lookback: int = 60, rebalance_days: int = 21,
                     max_weight: int = 40) -> list[dict[str, float]]:
    symbols = sorted(closes)
    cap = max_weight / 100.0
    out: list[dict[str, float]] = []

    for i in range(n_bars):
        if i % rebalance_days or i < lookback + 1:
            out.append({})
            continue
        inv = {}
        for s in symbols:
            v = _trailing_vol(closes[s], i, lookback)
            if v:
                inv[s] = 1.0 / v
        if not inv:
            out.append({})
            continue
        total = sum(inv.values())
        w = {s: x / total for s, x in inv.items()}
        # apply the cap, then renormalise what the cap freed up
        w = {s: min(cap, x) for s, x in w.items()}
        scale = sum(w.values())
        out.append({s: round(x / scale, 4) for s, x in w.items()} if scale else {})
    return out
