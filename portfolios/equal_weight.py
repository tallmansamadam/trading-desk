"""Equal weight, rebalanced — the benchmark everything else must beat.

This is 1/N. It is here as a strategy rather than only a yardstick because
DeMiguel, Garlappi and Uppal (2009) found that across fourteen datasets, no
optimising allocation rule reliably beat naive equal weighting once estimation
error was accounted for. Any clever scheme should be assumed inferior to this
until it demonstrates otherwise.

A portfolio rule returns, for each bar, a mapping of symbol -> weight. Weights
are fractions of equity; anything left unallocated is cash.
"""

NAME = "equal_weight"
PARAMS = {"rebalance_days": 21}   # roughly monthly


def generate_weights(closes: dict[str, list[float]], n_bars: int,
                     rebalance_days: int = 21) -> list[dict[str, float]]:
    symbols = sorted(closes)
    w = 1.0 / len(symbols)
    target = dict.fromkeys(symbols, w)
    # The engine only acts when weights CHANGE, so emitting the same dict every
    # bar means "rebalance back to equal weight on the rebalance cadence".
    out = []
    for i in range(n_bars):
        out.append(dict(target) if i % rebalance_days == 0 else {})
    return out
