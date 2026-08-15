#!/usr/bin/env python
"""trade.py — CLI for the IBKR agent team.

Every command opens a fresh session against TWS/IB Gateway, verifies the
account matches TRADING_MODE (paper vs live), does its work, disconnects.

Examples:
  python trade.py status
  python trade.py quote AAPL MSFT
  python trade.py bars SPY --duration "60 D" --size "1 day" --csv bars.csv
  python trade.py positions
  python trade.py pnl
  python trade.py check AAPL BUY 10
  python trade.py order AAPL BUY 10 --limit 180.50
  python trade.py cancel-all
  python trade.py flatten [SYMBOL]
  python trade.py halt / resume
"""

from __future__ import annotations

import argparse
import sys

from trading.config import HALT_FILE, load_settings, trading_halted


def cmd_halt(_args) -> None:
    HALT_FILE.write_text("Trading halted via trade.py halt\n")
    print(f"HALT file created at {HALT_FILE}. New orders are blocked (flatten still works).")


def cmd_resume(_args) -> None:
    if HALT_FILE.exists():
        HALT_FILE.unlink()
        print("HALT file removed. Trading resumed.")
    else:
        print("No HALT file present; trading was not halted.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("status", help="connection, mode, account balances")

    p = sub.add_parser("quote", help="snapshot quotes")
    p.add_argument("symbols", nargs="+")

    p = sub.add_parser("bars", help="historical bars")
    p.add_argument("symbol")
    p.add_argument("--duration", default="30 D")
    p.add_argument("--size", default="1 day")
    p.add_argument("--csv", help="write bars to CSV file")

    sub.add_parser("positions", help="positions + open orders")
    sub.add_parser("pnl", help="daily/unrealized/realized P&L")

    p = sub.add_parser("check", help="dry-run risk check (no order sent)")
    p.add_argument("symbol")
    p.add_argument("side", choices=["BUY", "SELL", "buy", "sell"])
    p.add_argument("quantity", type=float)
    p.add_argument("--limit", type=float, default=None)

    p = sub.add_parser("order", help="risk-checked order placement")
    p.add_argument("symbol")
    p.add_argument("side", choices=["BUY", "SELL", "buy", "sell"])
    p.add_argument("quantity", type=float)
    p.add_argument("--limit", type=float, default=None)
    p.add_argument("--tif", default="DAY", choices=["DAY", "GTC", "IOC"])
    p.add_argument("--confirm-live", action="store_true",
                   help="required (with LIVE_TRADING_ACK) for live orders")

    sub.add_parser("cancel-all", help="cancel all open orders")

    p = sub.add_parser("flatten", help="close positions with market orders")
    p.add_argument("symbol", nargs="?", default=None)
    p.add_argument("--confirm-live", action="store_true")

    sub.add_parser("halt", help="create HALT file (kill switch)")
    sub.add_parser("resume", help="remove HALT file")

    args = parser.parse_args()

    # Halt/resume are pure file operations — no connection needed.
    if args.command == "halt":
        return cmd_halt(args)
    if args.command == "resume":
        return cmd_resume(args)

    from trading.connection import ibkr_session
    from trading import market_data, orders, portfolio, risk

    settings = load_settings()
    if trading_halted() and args.command == "order":
        sys.exit("REFUSED: HALT file present. Use 'python trade.py resume' (human decision) first.")

    with ibkr_session(settings) as (ib, account, settings):
        if args.command == "status":
            print(portfolio.account_status(ib, account, settings.mode))
            print(f"Halted: {trading_halted()}  Market data type: {settings.market_data_type}")

        elif args.command == "quote":
            for row in market_data.snapshot(ib, args.symbols):
                print(
                    f"{row['symbol']:<8} bid={row['bid']} ask={row['ask']} "
                    f"last={row['last']} close={row['close']} vol={row['volume']}"
                )

        elif args.command == "bars":
            bars = market_data.historical_bars(ib, args.symbol, args.duration, args.size)
            if args.csv:
                import csv
                with open(args.csv, "w", newline="") as f:
                    writer = csv.writer(f)
                    writer.writerow(["date", "open", "high", "low", "close", "volume"])
                    for b in bars:
                        writer.writerow([b.date, b.open, b.high, b.low, b.close, b.volume])
                print(f"Wrote {len(bars)} bars to {args.csv}")
            else:
                for b in bars[-15:]:
                    print(f"{b.date}  O={b.open:<9} H={b.high:<9} L={b.low:<9} C={b.close:<9} V={b.volume}")
                if len(bars) > 15:
                    print(f"... ({len(bars)} bars total; use --csv for all)")

        elif args.command == "positions":
            print(portfolio.positions_table(ib, account))
            print()
            print(portfolio.open_orders_table(ib))

        elif args.command == "pnl":
            print(portfolio.pnl_summary(ib, account))

        elif args.command == "check":
            result = risk.check_order(
                ib, account, settings, args.symbol, args.side, args.quantity, args.limit
            )
            print(result.report())
            sys.exit(0 if result.approved else 1)

        elif args.command == "order":
            print(
                orders.place_order(
                    ib, account, settings, args.symbol, args.side, args.quantity,
                    limit_price=args.limit, tif=args.tif, confirm_live=args.confirm_live,
                )
            )

        elif args.command == "cancel-all":
            print(orders.cancel_all(ib))

        elif args.command == "flatten":
            print(orders.flatten(ib, account, settings, args.symbol, args.confirm_live))


if __name__ == "__main__":
    try:
        main()
    except (ConnectionError, TimeoutError) as exc:
        # TWS was reachable at connect time but the link dropped mid-request —
        # usually IBKR's nightly server reset, the machine sleeping, or TWS
        # being logged out. Crashing with a traceback here is the wrong failure
        # mode: an agent reading it gets Python internals instead of a state it
        # can act on, and after an 'order' the outcome is genuinely ambiguous.
        sys.exit(
            f"Lost the connection to TWS/IB Gateway mid-request ({type(exc).__name__}: {exc}).\n"
            "TWS may still be running while its link to IBKR is down — check the "
            "TWS window for a connection warning.\n"
            "IMPORTANT: if this happened during 'order' or 'flatten', the outcome "
            "is UNKNOWN. Do not retry blindly. Reconnect, then run "
            "'python trade.py positions' to see what actually reached the market."
        )
