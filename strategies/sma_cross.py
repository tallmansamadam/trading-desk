"""Example strategy: SMA crossover.

A strategy module must expose:
  NAME: str
  PARAMS: dict of defaults
  generate_signals(closes: list[float], **params) -> list[int]
    returns a target position per bar: 1 = long, 0 = flat
    (list must be the same length as closes)

The strategy-quant agent iterates on files in this directory and evaluates
them with backtest.py. Keep strategies pure (no I/O, no IBKR calls) so they
are trivially backtestable.
"""

NAME = "sma_cross"
PARAMS = {"fast": 10, "slow": 30}


def _sma(values: list[float], window: int) -> list[float | None]:
    out: list[float | None] = [None] * len(values)
    running = 0.0
    for i, v in enumerate(values):
        running += v
        if i >= window:
            running -= values[i - window]
        if i >= window - 1:
            out[i] = running / window
    return out


def generate_signals(closes: list[float], fast: int = 10, slow: int = 30) -> list[int]:
    fast_ma = _sma(closes, fast)
    slow_ma = _sma(closes, slow)
    signals = []
    for f, s in zip(fast_ma, slow_ma):
        if f is None or s is None:
            signals.append(0)
        else:
            signals.append(1 if f > s else 0)
    return signals
