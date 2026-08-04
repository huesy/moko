"""Tools — capabilities Moko can invoke during a turn (and, later, on its own).

An `AgentTool` pairs an LLM-facing spec (name/description/JSON-schema, reused
from the `llm` layer) with an async handler and a **risk** class + **category**.
The brain shows the specs to the model and dispatches calls to the handlers,
consulting the consent gate (`consent.py`) first: safe tools — read, search,
compute — run immediately; sensitive ones — send, spend, touch the system,
delete — need the companion's say-so (asked through the channel's approval flow).
The category lets the companion trust a whole class at once ("web is always
fine", "never auto-run system commands").
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from ..llm import Tool

SAFE = "safe"  # read-only / no side effects beyond local compute
SENSITIVE = "sensitive"  # sends, spends, touches the system, or is destructive


@dataclass(frozen=True)
class GateDecision:
    """A per-call gate verdict from a tool's own classifier. Lets a single tool
    (the shell) decide allow/ask/deny per *invocation* — e.g. by what command was
    asked for — instead of a fixed risk class for the whole tool."""

    verdict: str  # "allow" | "ask" | "deny" (consent.ALLOW / ASK / DENY)
    reason: str = ""  # human-facing why (shown in the approval + the monitor)
    tier: str = ""  # the classifier's internal tier, for logging/UI
    # The boundary *class* this action crosses ("network", "install",
    # "outside_write", …) — the unit the companion trusts when they pick "always".
    # Trusting a key frees that whole class without trusting the rest of the shell;
    # shared across surfaces (a shell redirect and write_file outside the workspace
    # both carry "outside_write", so one "always" frees both). Empty = nothing to
    # trust (a FREE action, or an unknown one that must always ask).
    key: str = ""


@dataclass(frozen=True)
class AgentTool:
    spec: Tool  # what the model sees (name, description, parameters)
    category: str  # "web" | "file" | "system" | "email" | …
    risk: str  # SAFE | SENSITIVE
    run: Callable[[dict], Awaitable[str]]  # the handler: args dict → result text
    # Optional per-call classifier. When set, the gate calls this with
    # (args, policy) and uses its verdict instead of the static category/risk —
    # how the shell tool classifies each command rather than gating them all.
    classify: Callable[[dict, dict[str, str]], GateDecision] | None = None
