# Trading Desk

A paper-trading system for US equities and crypto, built around a hard risk
engine and — more unusually — a validation harness whose job is to **reject**
strategies before they reach capital.

It runs against Interactive Brokers or Alpaca, sharing one risk engine between
them. It installs as a background service on Windows with a browser front end.
It has 100 tests.

**Its most useful output so far is a series of negative results.** That is the
point, and it is what the rest of this document is about.

---

## The headline: six strategies tested, six rejected

Most public trading repositories present a backtest with a rising equity curve.
This one presents the opposite, because the opposite is what the data supports.

| strategy | verdict | evidence |
|---|---|---|
| SMA crossover (10/30) | rejected | fails all five gates |
| SMA crossover (5/20) | rejected | in-sample +0.021 Sharpe → **out-of-sample −0.176**, t = −2.68 |
| VWAP scalp (intraday) | rejected | +0.248 bp/trade, t = 0.35, **p = 0.73** |
| Crypto scalp | rejected | 26–84 bp round-trip cost vs 1.7–5.4 bp median move — arithmetically impossible |
| Volatility targeting | rejected | t = 2.81 on 7 symbols, **1.41 on 24** — shrank as data grew |
| Inverse-vol allocation | rejected | loses to naive 1/N at **t = −4.27** |

The volatility-targeting result is the instructive one. On seven symbols it
cleared four of five gates and looked like a discovery. Widening to
twenty-four pre-specified symbols dropped its t-statistic from 2.81 to 1.41.

A real effect's t-statistic **grows** with sample size, roughly by √(n₂/n₁).
Going 7 → 24 symbols, that factor is 1.85×:

| | t @ 7 symbols | t @ 24 symbols | expected if real |
|---|---|---|---|
| lookahead control (known real edge) | 35.74 | **52.38** ↑ | 66.18 |
| volatility targeting | 2.81 | **1.41** ↓ | 5.20 |

The control grew. The candidate shrank. That is the signature of a null result,
and it is the difference between a finding and a story.

## What did survive

One thing, and it is not a strategy:

| | Sharpe | max drawdown |
|---|---|---|
| median single asset (of 24) | 0.58 | 37.2% |
| **equal weight, monthly rebalance** | **0.94** | **28.0%** |

Equal weighting beat 20 of the 24 constituents outright, with a shallower
drawdown than 19 of them, at 0.46× annual turnover. No forecast, no parameters,
no timing. It is the default mode of the live service for that reason.

---

## The validation harness

`validate.py` is the part worth reading first. A single good-looking backtest is
nearly meaningless: seven symbols times five parameter sets is thirty-five
chances to find a fluke, and this repository found several.

Five gates, each of which has killed something real here:

1. **GENERALISATION** — does it hold across a universe, or one lucky symbol?
   Requires t ≥ 2 on the cross-sectional Sharpe delta.
2. **OUT-OF-SAMPLE** — fit on the first half, verify on the second.
3. **RISK-MATCHED** — does it beat simply holding *less* of the same thing at
   equal drawdown? Scaling exposure cannot change Sharpe, so a timing rule that
   fails here is adding complexity for nothing.
4. **COST STRESS** — does it survive twice the assumed spread?
5. **PARAMETER PLATEAU** — is the chosen point surrounded by other working
   points, or is it a lone spike? A spike is a fit.

Verdicts are **three-state**. `INSUFFICIENT` is not a pass: a gate that could
not run leaves the strategy unproven, and unproven is not proven. A thin
universe blocks a pass rather than quietly granting one.

### The harness is calibrated

A validator that rejects everything is indistinguishable from a broken one.
`strategies/_control_lookahead.py` is a deliberate cheat — it reads the next
bar — and it **passes all five gates at t = 35.7**. That is what makes the
rejections meaningful rather than merely strict.

`validate_portfolio.py` applies the same idea to allocation rules, benchmarked
against naive 1/N following DeMiguel, Garlappi & Uppal (2009). Its null control
(equal weight against itself) returns exactly `+0.000` on every gate, which is
what proves the rule and the benchmark share an engine.

---

## Risk architecture

Limits are enforced in code, not in configuration or convention. Every order
from every entry point — two CLIs, the autonomous loop, and the GUI — passes
the same `trading/risk.py`.

- **Paper by default.** Live requires all three of `TRADING_MODE=live`, a
  human-set `LIVE_TRADING_ACK`, and `--confirm-live` on the specific command.
  The unattended service refuses live outright regardless.
- **Broker/mode mismatch aborts before anything is sent.** A paper account
  (`DU`/`DF`) under live mode, or the reverse, halts at connection.
- **Two-tier notional caps.** `MAX_POSITION_NOTIONAL` per symbol,
  `MAX_GROSS_NOTIONAL` book-wide. Without the second, N symbols can each sit at
  their cap and no check objects — a 10-year replay reached $104,570 gross on a
  $100k account exactly that way.
- **Both caps are ENTRY caps, and the code says so.** Nothing trims a position
  that drifts past its limit on appreciation; the same replay had one holding
  reach 4.9× its cap without any order breaking a rule. The gross check
  therefore **always lets a de-risking order through**, because blocking those
  would trap an oversized book with no way down.
- **Kill switch.** A `HALT` file blocks new entries instantly while still
  permitting flattening and reporting. Reachable from the GUI in one click.
- **Agents cannot edit their own limits.** Permission rules plus a `PreToolUse`
  hook block both the tool path and the shell path to `.env`, `risk.py` and
  `config.py`.

### One risk engine, two brokers

`trade.py` drives IBKR; `trade_alpaca.py` drives Alpaca. They share the risk
engine, the limits, and the kill switch — swapping brokers must never mean
swapping safety.

That works because the engine is duck-typed: it needs only an object exposing
`positions()`, `portfolio()`, `reqAllOpenOrders()`, `reqPnL()`, `cancelPnL()`
and `sleep()`. `trading/brokers/alpaca.py` presents that surface over REST.
Adding a broker means writing an adapter, never touching `risk.py`.

---

## Backtest correctness

The engine is where a backtest quietly lies, so its behaviour is pinned by
tests rather than asserted in prose.

- **Execution lag is exactly one bar.** A signal computed on bar *i*'s close is
  acted on for bar *i+1* — never zero (lookahead), never two (understated).
  `tests/test_backtest.py` fails if either drifts. It caught a real two-bar
  regression during development.
- **Lookahead detection is itself tested.** A sensitivity test confirms the
  suite *can* detect leakage, so a clean run means something.
- **Costs are charged on the change in exposure**, so binary and fractional
  strategies are costed consistently.
- **Intrabar ambiguity resolves pessimistically.** Where a minute bar spans both
  stop and target, `backtest_scalp.py` assumes the stop filled first. A bar
  cannot say which came first, and the pessimistic reading is the honest one.
- **Total return, not price return.** Bar CSVs carry dividend/split-adjusted
  closes and the engine prefers them.

---

## Running it

```bash
pip install -r requirements.txt
cp .env.example .env          # then edit: limits and broker keys
```

```bash
python validate.py sma_cross              # gate a strategy
python trade_alpaca.py check SPY BUY 5    # dry-run risk check, sends nothing
python run_desk.py --once                 # allocator, shadow mode
python desk_service.py                    # service + dashboard on :6400
```

Windows service install (per-user scheduled task, no admin):

```powershell
.\install_service.ps1            # install, starts at logon
.\install_service.ps1 -Status    # registered? running? armed?
.\install_service.ps1 -Uninstall
```

The service starts **disarmed**. Arming is a decision made while looking at the
state, not a side effect of a setup script.

---

## What is verified, and what is not

Stated plainly, because a system that overstates its own assurance is worse
than one that does less.

**Verified against a live broker:** connection and paper/live guard, account
and position reporting, quotes and historical bars, the full risk engine
(approve, restricted-symbol reject, oversized reject, gross-exposure against a
real portfolio response), order placement, resting, and cancellation.

**Verified by test only:** the IBKR path beyond connection — TWS was
unavailable for most of development, so IBKR order placement is exercised
against stubs and checked against `ib_async` signatures, not observed.

**Not verified:** any claim about future profitability. The allocator's Sharpe
0.94 is a backtest over one decade of a strong equity market. Backtests are
hypotheses.

---

## Layout

```
trade.py trade_alpaca.py     broker CLIs, one risk engine
trading/                     config, risk, orders, portfolio, market data, news
trading/brokers/             broker adapters — add here, not in risk.py
strategies/ portfolios/      signal and allocation rules
validate.py                  five gates for timing rules
validate_portfolio.py        five gates for allocation rules
backtest.py                  daily engine, one-bar lag, costs modelled
backtest_scalp.py            intraday engine, intrabar stops
simulate.py                  replay one symbol through the real risk engine
simulate_portfolio.py        replay a shared book through it
run_desk.py                  autonomous loop: allocate or scalp
desk_service.py              installed service: scheduler + GUI + controls
dashboard_ui.html            the front end
tests/                       100 tests
```

## Notes

Not investment advice, and nothing here is a recommendation to trade anything.
Paper by default; live trading requires deliberate, multi-step opt-in.

The `.claude/` directory contains an agent team and the permission rules and
hooks that constrain it — including the ones that stop an agent editing the
risk limits it operates under.
