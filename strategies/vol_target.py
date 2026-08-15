"""Volatility targeting — hold a constant amount of RISK, not a constant
amount of the instrument.

This forecasts nothing about direction, which is the point. Every directional
rule tried in this repo failed the generalisation gate, because next period's
return is close to unforecastable from price history. Next period's VOLATILITY
is a different matter: volatility clusters, strongly and almost universally.
Quiet weeks follow quiet weeks. That autocorrelation is one of the most durable
regularities in markets, and unlike a return forecast it does not require
anyone to be wrong for you to be right.

The rule:

    exposure = clamp(target_vol / trailing_realised_vol, 0, cap)

When the market is calm you hold more of it; when it is violent you hold less.
Position changes are gradual, so turnover stays low and costs stay small.

Why this can beat constant exposure at all: scaling a position up or down
changes return and volatility in the same proportion, so it CANNOT change
Sharpe. Anything that does change Sharpe must vary exposure against something
predictable. Volatility is predictable. Direction is not.

Nothing here is guaranteed to work — run validate.py and let the gates decide.
"""

import math

NAME = "vol_target"
PARAMS = {
    "lookback": 20,        # trading days in the volatility estimate
    "target_vol": 12,      # desired annualised volatility, in percent
    "cap": 100,            # max exposure in percent (100 = never leveraged)
}


def realised_vol(closes: list[float], lookback: int) -> list[float | None]:
    """Trailing annualised volatility, using only data available at each bar."""
    out: list[float | None] = [None]
    rets: list[float] = []
    for i in range(1, len(closes)):
        prev = closes[i - 1]
        rets.append((closes[i] / prev - 1) if prev else 0.0)
        if len(rets) > lookback:
            rets.pop(0)
        if len(rets) < lookback:
            out.append(None)
            continue
        mean = sum(rets) / len(rets)
        var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
        out.append(math.sqrt(var) * math.sqrt(252))
    return out


def generate_signals(closes: list[float], lookback: int = 20,
                     target_vol: int = 12, cap: int = 100) -> list[float]:
    """Fractional exposure per bar, in [0, cap/100]."""
    target = target_vol / 100.0
    ceiling = cap / 100.0
    vols = realised_vol(closes, lookback)
    signals: list[float] = []
    for v in vols:
        if v is None or v <= 1e-9:
            signals.append(0.0)          # no estimate yet, so no position
            continue
        # round the exposure so tiny wobbles do not generate pointless turnover
        signals.append(round(min(ceiling, target / v), 2))
    return signals
