"""Central configuration for the IBKR trading toolkit.

Everything is driven by environment variables (optionally loaded from a .env
file in the repo root) so agents never hard-code credentials or limits.

TRADING_MODE is the master switch:
  paper (default) -> TWS paper port 7497 (or IB Gateway 4002 via IBKR_PORT)
  live            -> refused unless LIVE_TRADING_ACK is also set correctly
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
HALT_FILE = REPO_ROOT / "HALT"

# The exact string a human must place in the environment before any live
# order is possible. Agents must never set this themselves.
LIVE_ACK_PHRASE = "I_UNDERSTAND_THIS_TRADES_REAL_MONEY"

_DEFAULT_PORTS = {"paper": 7497, "live": 7496}  # TWS; Gateway uses 4002/4001


def _load_dotenv() -> None:
    """Tiny .env loader (KEY=VALUE lines) so we don't need a dependency."""
    env_path = REPO_ROOT / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip())


@dataclass(frozen=True)
class Settings:
    mode: str = "paper"
    host: str = "127.0.0.1"
    port: int = 7497
    client_id: int = 17
    account: str = ""          # blank = first managed account
    market_data_type: int = 3  # 1=live, 2=frozen, 3=delayed, 4=delayed-frozen

    # Risk limits (enforced in trading/risk.py before every order)
    max_order_notional: float = 5_000.0
    max_position_notional: float = 10_000.0
    # Ceiling on the whole book. max_position_notional is PER SYMBOL, so without
    # this N symbols can each sit at their cap and nothing objects.
    # 70k on a 100k account: a 10-year 7-symbol replay showed this costs ~2% of
    # P&L versus no cap while cutting peak gross exposure by ~24%.
    max_gross_notional: float = 70_000.0
    max_open_orders: int = 10
    max_daily_loss: float = 500.0
    restricted_symbols: tuple[str, ...] = field(default_factory=tuple)

    @property
    def is_live(self) -> bool:
        return self.mode == "live"

    @property
    def live_ack_present(self) -> bool:
        return os.environ.get("LIVE_TRADING_ACK", "") == LIVE_ACK_PHRASE


def load_settings() -> Settings:
    _load_dotenv()
    mode = os.environ.get("TRADING_MODE", "paper").strip().lower()
    if mode not in ("paper", "live"):
        raise SystemExit(f"TRADING_MODE must be 'paper' or 'live', got {mode!r}")

    port = int(os.environ.get("IBKR_PORT", _DEFAULT_PORTS[mode]))
    restricted = tuple(
        s.strip().upper()
        for s in os.environ.get("RESTRICTED_SYMBOLS", "").split(",")
        if s.strip()
    )
    return Settings(
        mode=mode,
        host=os.environ.get("IBKR_HOST", "127.0.0.1"),
        port=port,
        client_id=int(os.environ.get("IBKR_CLIENT_ID", "17")),
        account=os.environ.get("IBKR_ACCOUNT", ""),
        market_data_type=int(os.environ.get("IBKR_MKT_DATA_TYPE", "3")),
        max_order_notional=float(os.environ.get("MAX_ORDER_NOTIONAL", "5000")),
        max_position_notional=float(os.environ.get("MAX_POSITION_NOTIONAL", "10000")),
        max_gross_notional=float(os.environ.get("MAX_GROSS_NOTIONAL", "70000")),
        max_open_orders=int(os.environ.get("MAX_OPEN_ORDERS", "10")),
        max_daily_loss=float(os.environ.get("MAX_DAILY_LOSS", "500")),
        restricted_symbols=restricted,
    )


def trading_halted() -> bool:
    return HALT_FILE.exists()
