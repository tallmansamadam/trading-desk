# IBKR automated trading desk

An agent team that researches, backtests, risk-checks and executes trades
through Interactive Brokers. Paper trading by default.

## Non-negotiables

1. **Paper unless a human says otherwise.** `TRADING_MODE=paper` is the default.
   Live orders require all three: `TRADING_MODE=live`, the `LIVE_TRADING_ACK`
   env var set by a human, and `--confirm-live` on the specific order command.
2. **Only `execution-trader` places orders.** Every other agent, including the
   orchestrator, is read-only with respect to order entry.
3. **Every order passes `risk-manager` first.** The risk engine also re-runs
   inside `trade.py order` as a backstop.
4. **No agent edits risk limits.** `.env`, `trading/risk.py` and
   `trading/config.py` are off limits, enforced two ways: permission deny rules
   cover the Read/Edit/Write tools, and a `PreToolUse` hook
   (`.claude/hooks/guard_risk_files.py`) blocks the shell path — redirection,
   `sed -i`, `cp`/`mv`, `tee`, PowerShell `*-Item`/`*-Content`. If a limit
   blocks a trade, the trade is rejected. Relay the request to the human.
5. **`python trade.py halt` on anything unexplained.** Halting blocks new orders
   but still allows flattening and reporting. Only a human resumes.

## The team

| Agent | Role | Can trade? |
|---|---|---|
| `trading-orchestrator` | Routes work, enforces the pipeline | No |
| `market-analyst` | Quotes, bars, trend, levels, volatility | No |
| `strategy-quant` | Writes and backtests strategies | No |
| `risk-manager` | Sizes positions, approves or rejects | No |
| `execution-trader` | Places, cancels, flattens orders | **Yes** |
| `portfolio-monitor` | Positions, P&L, alerts, kill switch | No |

Pipeline: research → backtest → risk check → execute → monitor.

## Commands

```
python trade.py status | quote SYM | bars SYM | positions | pnl
python trade.py check SYM BUY 10          # dry-run risk check, sends nothing
python trade.py order SYM BUY 10 --limit 180.50
python trade.py cancel-all | flatten [SYM]
python trade.py halt | resume
python backtest.py sma_cross SPY --duration "2 Y"      # bars from IBKR
python backtest.py sma_cross SPY --csv data/SPY.csv    # bars from disk
python fetch_data.py SPY QQQ --range 10y               # offline test data
```

## Data

`data/` holds daily CSVs for backtesting without TWS running. Refresh with
`python fetch_data.py SYM...`. These come from a public provider and are for
**testing only** — anything you actually trade against should use IBKR bars via
`python trade.py bars SYM --csv out.csv`.

CSVs carry both `close` and dividend/split-adjusted `adjclose`; `backtest.py`
prefers `adjclose` when present, so returns are total-return.

## Layout

- `trade.py` — CLI the agents drive
- `backtest.py` — strategy evaluation, next-bar execution, costs modeled
- `fetch_data.py` — downloads daily bars for offline backtesting
- `trading/` — `config`, `connection`, `market_data`, `risk`, `orders`, `portfolio`
- `strategies/` — pure signal functions, one per file
- `data/` — downloaded bar CSVs (gitignored)
- `tests/` — safety and backtest-engine tests (`python -m unittest discover tests`)
- `.claude/agents/` — the six agent definitions
- `.claude/hooks/` — `guard_risk_files.py`, the shell-path guard on risk controls

## Risk limits are two-tier — and both are ENTRY caps

`MAX_POSITION_NOTIONAL` bounds a single symbol; `MAX_GROSS_NOTIONAL` bounds the
whole book. Without the second, N symbols can each sit at the per-symbol cap and
no check objects — a 10-year 7-symbol replay reached $104,570 gross on a $100k
account that way.

Both are checked **at order time only**. Nothing trims a position that drifts
past its cap on appreciation, so gross can exceed the ceiling without any order
having broken a rule (that same replay had AAPL reach 4.9x its cap). The gross
check therefore always lets a **de-risking order through** — blocking those would
trap an oversized book with no way down. `portfolio-monitor` watches for the
drift; only `execution-trader` can act on it.

## Pricing outside market hours

With delayed data (`IBKR_MKT_DATA_TYPE=3`) there is no streaming quote once the
session closes. `reference_price` then falls back to the most recent daily bar
and prints a loud STALE warning. An overnight gap makes that an underestimate of
true notional, so **pass `--limit` for anything sized off-hours** — a limit price
skips the fallback entirely, because the limit is what gets sized.

## Execution model

A signal computed on bar *i*'s close is acted on for bar *i+1* — one bar of lag,
never zero (lookahead) and never two (understated). `tests/test_backtest.py`
pins this exactly; if you touch `run_backtest`, those tests must still pass.

## Requirements

TWS or IB Gateway running and logged in, with the API enabled
(TWS: File → Global Configuration → API → Settings → *Enable ActiveX and Socket
Clients*, and add `127.0.0.1` to trusted IPs). `pip install -r requirements.txt`.

Connections verify that the account prefix matches the mode — a paper account
(`DU`/`DF`) under `TRADING_MODE=live`, or a live account under `paper`, aborts
before anything is sent.
