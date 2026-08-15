"""Connection management for TWS / IB Gateway with a paper/live safety check.

IBKR paper account ids start with 'D' (DU/DF prefixes). After connecting we
verify the account matches TRADING_MODE so a mis-set port can never route a
paper-intended order to a live account, or vice versa.
"""

from __future__ import annotations

import contextlib

from ib_async import IB

from .config import Settings, load_settings


class ModeMismatchError(SystemExit):
    pass


def _resolve_account(ib: IB, settings: Settings) -> str:
    accounts = ib.managedAccounts()
    if not accounts:
        raise SystemExit("No managed accounts returned by TWS/Gateway.")
    if settings.account:
        if settings.account not in accounts:
            raise SystemExit(
                f"IBKR_ACCOUNT={settings.account} not in managed accounts {accounts}"
            )
        return settings.account
    return accounts[0]


def _verify_mode(account: str, settings: Settings) -> None:
    is_paper_account = account.upper().startswith("D")
    if settings.is_live and is_paper_account:
        raise ModeMismatchError(
            f"TRADING_MODE=live but connected account {account} is a PAPER account. "
            "Fix IBKR_PORT / TRADING_MODE before continuing."
        )
    if not settings.is_live and not is_paper_account:
        raise ModeMismatchError(
            f"TRADING_MODE=paper but connected account {account} looks LIVE. "
            "Refusing to continue. Check that TWS/Gateway is logged into the "
            "paper environment and IBKR_PORT points at it (TWS 7497 / GW 4002)."
        )


@contextlib.contextmanager
def ibkr_session(settings: Settings | None = None):
    """Yield (ib, account, settings) with the mode/account check done."""
    settings = settings or load_settings()
    ib = IB()
    try:
        ib.connect(
            settings.host,
            settings.port,
            clientId=settings.client_id,
            timeout=10,
            readonly=False,
        )
    except Exception as exc:  # ConnectionRefusedError, TimeoutError, etc.
        raise SystemExit(
            f"Could not connect to TWS/IB Gateway at {settings.host}:{settings.port} "
            f"({exc}). Is it running with the API enabled? "
            "(TWS: File > Global Configuration > API > Settings > "
            "'Enable ActiveX and Socket Clients')"
        ) from exc

    try:
        account = _resolve_account(ib, settings)
        _verify_mode(account, settings)
        ib.reqMarketDataType(settings.market_data_type)
        yield ib, account, settings
    finally:
        if ib.isConnected():
            ib.disconnect()
