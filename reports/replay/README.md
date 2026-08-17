# Replay baselines

Committed output from `replay.py` — the real `Service` + `Allocator` + risk
engine run over historical bars through a replay broker.

```
python replay.py --start 2022-01-03 --end 2022-12-30 --pathology --save 2022-bear
```

These are **behavioural regression baselines, not performance claims.** The
replay is deterministic: the same data and flags reproduce a byte-identical
summary. So a diff in one of these files means *the desk decides differently
than it used to* — which is either a change you intended, or a regression you
would otherwise have discovered in production.

| baseline | period | what it stresses |
|---|---|---|
| `covid-crash` | 2020-01 → 2020-06 | a violent gap down and a fast recovery; the daily-loss rail fires ~108 times |
| `2017-meltup` | 2017 | a low-volatility rise; the desk trades little and drifts +4.9% PAST the gross cap |
| `2018-vol` | 2018 | a year where every asset class finished negative, ending in a −13.8% Q4; the desk barely trades |
| `2022-bear` | 2022 | a slow grind where stocks *and* bonds fall together, so diversification cannot rescue the year |
| `decade` | 2016-08 → present | the long run, including every rebalance cadence and drift trigger |

## What the numbers are and are not

The returns are **not** evidence of a strategy. They are one path, on one
universe, over a period already known to the person who chose it. Read them as
"the desk behaved coherently across this regime", not "the desk makes money".

Execution is modelled **optimistically**: fills are all-or-nothing at the
touch, with no partials, no queue position and no slippage beyond the limit.
Real fills would be worse. The fill model is documented in
`trading/brokers/replay.py`.

### The EXPOSURE finding is expected, not a bug

Three baselines finish over the gross cap: 2017 (+4.9%), the decade (+4.9%) and
COVID (+6.1%). Both caps are ENTRY caps — they are checked when an order is
placed and nothing trims a position that appreciates past its ceiling. So a
rising market carries the book over, and a falling one pulls it back under,
which is exactly the pattern across these five files.

That is documented behaviour rather than a defect, but it is reported every
time, because a limit the system cannot enforce after entry is precisely the
kind of thing that should stay visible.

The pathology line is the part that matters most. It checks things a unit test
cannot, because they are about behaviour over *time*: rebalance spin, per-name
churn, unfilled ratio, ending exposure over the cap, negative cash, broker
rejects and unhandled errors.
