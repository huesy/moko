"""The consent gate's policy — per-category trust over the risk floor.

Default: safe actions run; sensitive ones must be asked. The companion can raise
or pin trust per category (e.g. "web: always allow", "system: always ask",
"file: never"). Sensitive actions are asked through the channel's approval flow,
and the companion's per-category choices persist (see `config/policy.py`).
"""

from __future__ import annotations

from .base import SAFE

ALLOW = "allow"  # run now
ASK = "ask"  # name intent, get the companion's yes, then run
DENY = "deny"  # never, automatically


def decide(category: str, risk: str, policy: dict[str, str] | None = None) -> str:
    """How to handle a tool call: a per-category policy override wins; otherwise
    safe → allow, sensitive → ask. The mission's floor (sensitive is never silent)
    holds unless the companion has explicitly set a category's policy."""
    policy = policy or {}
    if category in policy:
        return policy[category]
    return ALLOW if risk == SAFE else ASK
