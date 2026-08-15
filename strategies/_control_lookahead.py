"""POSITIVE CONTROL FOR validate.py — NOT A TRADEABLE STRATEGY.

This cheats. It reads the next bar's close, which is information that does not
exist when the decision is made. It exists for exactly one reason: a validator
that rejects everything is indistinguishable from a validator that is broken.
Feeding it a signal with a real, large, universal edge proves the gates can
actually be passed, so a REJECT elsewhere means something.

The leading underscore keeps it out of casual tabbing. If this ever appears in
a live configuration, that is a bug of the most serious kind.

    python validate.py _control_lookahead     -> should PASS every gate
"""

NAME = "_control_lookahead"
PARAMS = {"edge": 60}   # percent of the future it is allowed to see


def generate_signals(closes: list[float], edge: int = 60) -> list[int]:
    """Long when the NEXT bar rises — deliberate lookahead.

    `edge` dilutes the cheat: at 100 it is perfect foresight, at 50 it is a coin.
    Kept well below 100 so the control is a strong-but-not-absurd edge, which is
    a fairer test of the gates than an infinite one.
    """
    out = []
    for i in range(len(closes)):
        nxt = i + 1
        if nxt >= len(closes):
            out.append(0)
            continue
        rising = closes[nxt] > closes[i]
        # deterministic dilution: let a fixed fraction of bars through honestly
        cheat = (i * 37) % 100 < edge
        out.append(1 if (rising if cheat else False) else 0)
    return out
