#!/usr/bin/env python
"""PreToolUse hook: block shell access to the trading risk controls.

The permission rules in .claude/settings.json cover the Read/Edit/Write tools.
This closes the shell path — no agent may reach .env, trading/risk.py or
trading/config.py through redirection, sed -i, cp/mv, tee, or the PowerShell
*-Item / *-Content cmdlets.

Failure policy: fails OPEN when there is nothing inspectable (malformed input,
no command field) because blocking every shell call on a parse bug is worse than
the residual risk — the permission deny rules are still the primary control.
Fails CLOSED whenever a protected path actually matches.
"""

from __future__ import annotations

import json
import re
import sys

# `.env` but not `.env.example` / `.envrc`
ENV_RE = re.compile(r"\.env(?![.\w])")

RISK_RE = re.compile(r"trading[/\\](risk|config)\.py")

WRITE_RE = re.compile(
    r">|"                                            # any redirection
    r"\bsed\b[^|;]*-i|"
    r"\bperl\b[^|;]*-i|"
    r"\b(tee|cp|mv|rm|truncate|dd|patch|install)\b|"
    r"\bgit\s+(checkout|apply|restore|stash|clean)\b|"
    r"\b(Set-Content|Add-Content|Out-File|New-Item|Remove-Item|Copy-Item|Move-Item|Clear-Content)\b",
    re.IGNORECASE,
)

ENV_REASON = (
    "Blocked by the guard_risk_files hook: .env holds the trading risk limits "
    "(MAX_ORDER_NOTIONAL, MAX_POSITION_NOTIONAL, MAX_DAILY_LOSS, "
    "RESTRICTED_SYMBOLS, LIVE_TRADING_ACK). No agent may reach it through the "
    "shell — a human edits it directly. If a risk limit is blocking a trade, "
    "report that to the human rather than trying to change the limit."
)

RISK_REASON = (
    "Blocked by the guard_risk_files hook: trading/risk.py and trading/config.py "
    "implement the pre-trade risk checks. Agents must not modify them through the "
    "shell. Use the Read tool if you need to understand what a limit does."
)


def deny(reason: str) -> None:
    json.dump(
        {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": reason,
            }
        },
        sys.stdout,
    )
    sys.stdout.write("\n")
    sys.exit(0)


def main() -> None:
    try:
        payload = json.load(sys.stdin)
        command = (payload.get("tool_input") or {}).get("command") or ""
    except Exception:
        sys.exit(0)  # nothing to inspect — permission rules still apply

    if not isinstance(command, str) or not command.strip():
        sys.exit(0)

    if ENV_RE.search(command):
        deny(ENV_REASON)

    if RISK_RE.search(command) and WRITE_RE.search(command):
        deny(RISK_REASON)

    sys.exit(0)


if __name__ == "__main__":
    main()
