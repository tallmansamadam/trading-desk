#!/usr/bin/env python
"""trade_alpaca.py — the same desk, against Alpaca instead of IBKR.

Command-for-command equivalent of trade.py. Crucially it shares the SAME risk
engine (trading/risk.py), the SAME limits from .env and the SAME HALT kill
switch — swapping brokers must never mean swapping safety.

Setup:
  1. Create PAPER keys at https://app.alpaca.markets (Paper Trading -> API Keys)
  2. Put them in .env (a human edits that file, never an agent):
       ALPACA_API_KEY=...
       ALPACA_SECRET_KEY=...
  3. python trade_alpaca.py status

Examples:
  python trade_alpaca.py status
  python trade_alpaca.py quote AAPL MSFT
  python trade_alpaca.py bars SPY --timeframe 1Day --limit 30
  python trade_alpaca.py positions
  python trade_alpaca.py pnl
  python trade_alpaca.py check AAPL BUY 10
  python trade_alpaca.py order AAPL BUY 10 --limit 180.50
  python trade_alpaca.py cancel-all
  python trade_alpaca.py flatten [SYMBOL]

halt/resume live in trade.py and are shared — the HALT file is broker-agnostic.
"""

from __future__ import annotations

import argparse
import sys

from trading import portfolio, risk
from trading.brokers.alpaca import AlpacaBroker, AlpacaError
from trading.config import load_settings, trading_halted

# Windows consoles default to cp1252, which cannot encode characters the risk
# report uses (e.g. the U+2248 "almost equal" sign). That raises
# UnicodeEncodeError the moment output is piped or redirected — precisely how an
# agent or a log capture reads it. Force UTF-8 on the way out.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("status", help="connection, mode, account balances")

    p = sub.add_parser("quote", help="latest trade/quote")
    p.add_argument("symbols", nargs="+")

    p = sub.add_parser("bars", help="historical bars")
    p.add_argument("symbol")
    p.add_argument("--timeframe", default="1Day", help="1Min, 5Min, 1Hour, 1Day")
    p.add_argument("--limit", type=int, default=30)

    sub.add_parser("positions", help="positions + open orders")
    sub.add_parser("pnl", help="daily / unrealized P&L")

    p = sub.add_parser("check", help="dry-run risk check (sends nothing)")
    p.add_argument("symbol")
    p.add_argument("side", choices=["BUY", "SELL", "buy", "sell"])
    p.add_argument("quantity", type=float)
    p.add_argument("--limit", type=float, default=None)

    p = sub.add_parser("order", help="risk-checked order placement")
    p.add_argument("symbol")
    p.add_argument("side", choices=["BUY", "SELL", "buy", "sell"])
    p.add_argument("quantity", type=float)
    p.add_argument("--limit", type=float, default=None)
    p.add_argument("--tif", default="day", choices=["day", "gtc", "ioc", "fok"])
    p.add_argument("--confirm-live", action="store_true",
                   help="required (with LIVE_TRADING_ACK) for live orders")

    sub.add_parser("cancel-all", help="cancel all open orders")

    p = sub.add_parser("flatten", help="close positions at market")
    p.add_argument("symbol", nargs="?", default=None)
    p.add_argument("--confirm-live", action="store_true")

    return parser


def main() -> None:
    args = build_parser().parse_args()
    settings = load_settings()

    if trading_halted() and args.command == "order":
        sys.exit("REFUSED: HALT file present. Use 'python trade.py resume' "
                 "(human decision) first.")

    if settings.is_live and args.command in ("order", "flatten"):
        if not settings.live_ack_present:
            sys.exit("REFUSED: TRADING_MODE=live but LIVE_TRADING_ACK is not set "
                     "by a human. Live orders are refused.")
        if not args.confirm_live:
            sys.exit("REFUSED: live orders also require --confirm-live on this "
                     "specific command.")

    broker = AlpacaBroker(mode=settings.mode)
    account = broker.connect_and_verify()

    if args.command == "status":
        print(portfolio.account_status(broker, account, settings.mode))
        print(f"Halted: {trading_halted()}  "
              f"Market open: {broker.is_market_open()}  "
              f"Feed: {broker.feed or 'account default'}")

    elif args.command == "quote":
        for row in broker.snapshot(args.symbols):
            print(f"{row['symbol']:<8} bid={row['bid']} ask={row['ask']} "
                  f"last={row['last']}")

    elif args.command == "bars":
        bars = broker.historical_bars(args.symbol, args.timeframe, args.limit)
        if not bars:
            sys.exit(f"No bars returned for {args.symbol.upper()}. On the free "
                     "IEX feed some symbols and timeframes are sparse.")
        for b in bars[-15:]:
            print(f"{b['t'][:10]}  O={b['o']:<9} H={b['h']:<9} "
                  f"L={b['l']:<9} C={b['c']:<9} V={b['v']}")
        if len(bars) > 15:
            print(f"... ({len(bars)} bars total)")

    elif args.command == "positions":
        print(portfolio.positions_table(broker, account))
        print()
        print(portfolio.open_orders_table(broker))

    elif args.command == "pnl":
        print(portfolio.pnl_summary(broker, account))

    elif args.command == "check":
        result = risk.check_order(broker, account, settings, args.symbol,
                                  args.side, args.quantity, args.limit)
        print(result.report())
        sys.exit(0 if result.approved else 1)

    elif args.command == "order":
        result = risk.check_order(broker, account, settings, args.symbol,
                                  args.side, args.quantity, args.limit)
        print(result.report())
        if not result.approved:
            sys.exit("REFUSED: risk checks failed — order not sent.")
        trade = broker.place_order(args.symbol, args.side, args.quantity,
                                   args.limit, args.tif)
        s = trade.orderStatus
        pending = s.status in ("new", "pending_new", "accepted")
        detail = ("broker has not reported a fill yet" if pending else
                  f"filled={s.filled:,.0f} remaining={s.remaining:,.0f} "
                  f"avgFillPrice={s.avgFillPrice:.2f}")
        print(f"Order sent [ALPACA {settings.mode.upper()}] {args.side.upper()} "
              f"{args.quantity} {args.symbol.upper()} "
              f"{f'@ {args.limit:.2f} LMT' if args.limit else 'MKT'} "
              f"tif={args.tif} | id={trade.order.orderId} "
              f"status={s.status} | {detail}")

    elif args.command == "cancel-all":
        count = broker.cancel_all()
        print(f"Cancelled {count} open order(s)." if count else "No open orders.")

    elif args.command == "flatten":
        targets = [p for p in broker.positions()
                   if args.symbol is None
                   or p.contract.symbol == args.symbol.upper()]
        if not targets:
            print("Nothing to flatten.")
        else:
            for pos in targets:
                broker.close_position(pos.contract.symbol)
                print(f"Closing {pos.position:+.0f} {pos.contract.symbol}")
            if trading_halted():
                print("(note: HALT file present — flatten allowed, entries blocked)")


if __name__ == "__main__":
    try:
        main()
    except AlpacaError as exc:
        sys.exit(str(exc))
    except (ConnectionError, TimeoutError) as exc:
        sys.exit(
            f"Lost the connection to Alpaca mid-request ({type(exc).__name__}: {exc}).\n"
            "IMPORTANT: if this happened during 'order' or 'flatten', the outcome "
            "is UNKNOWN. Do not retry blindly. Run "
            "'python trade_alpaca.py positions' to see what actually reached the market."
        )
