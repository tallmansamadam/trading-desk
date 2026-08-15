"""Intraday VWAP mean-reversion scalp.

The premise: within a session, price oscillates around volume-weighted average
price. When it stretches far enough below VWAP without a regime break, it tends
to snap back. That is the classic scalp — small edge, many repetitions, and it
lives or dies on costs.

Entry signal only. The backtest owns the exits (target, stop, timeout, flat by
the close) because a scalp is defined at least as much by how it gets out.

Bars are dicts with t/o/h/l/c/v as written by fetch_intraday.py.
"""

NAME = "scalp_vwap"
PARAMS = {
    "z_entry": 2.0,     # std devs below VWAP to open
    "lookback": 30,     # bars in the deviation std estimate
    "warmup": 15,       # bars into the session before trusting VWAP
}


def session_key(ts: str) -> str:
    return ts[:10]


def compute_vwap_z(bars: list[dict], lookback: int, warmup: int) -> list[float | None]:
    """Z-score of (close - session VWAP), reset each session."""
    out: list[float | None] = []
    cum_pv = cum_v = 0.0
    devs: list[float] = []
    current = None

    for b in bars:
        day = session_key(b["t"])
        if day != current:            # new session: VWAP and stats restart
            current, cum_pv, cum_v, devs = day, 0.0, 0.0, []
            bars_in = 0
        typical = (b["h"] + b["l"] + b["c"]) / 3
        vol = max(b["v"], 1.0)
        cum_pv += typical * vol
        cum_v += vol
        vwap = cum_pv / cum_v
        dev = b["c"] - vwap
        devs.append(dev)
        if len(devs) > lookback:
            devs.pop(0)
        bars_in = len(devs)

        if bars_in < max(warmup, 5):
            out.append(None)
            continue
        mean = sum(devs) / len(devs)
        var = sum((d - mean) ** 2 for d in devs) / max(len(devs) - 1, 1)
        sd = var ** 0.5
        out.append(None if sd <= 1e-9 else dev / sd)
    return out


def generate_signals(bars: list[dict], z_entry: float = 2.0, lookback: int = 30,
                     warmup: int = 15) -> list[int]:
    """1 = open a long on the next bar, 0 = no new entry."""
    z = compute_vwap_z(bars, lookback, warmup)
    return [1 if (v is not None and v <= -z_entry) else 0 for v in z]
