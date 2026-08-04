"""`moko trust` — view or set what Moko may do without asking.

  moko trust                      # show current trust
  moko trust network allow        # stop asking for downloads (curl/wget/git pull)
  moko trust install ask          # go back to asking for installs (the default)
  moko trust shell deny           # global off-switch: gate every shell command

Trust is by **boundary class** — the kind of crossing, not the tool — so trusting
one class never trusts the rest of the shell. The same thing the "👍 Always"
button sets. Trustable classes: network (downloads), install (package installs),
runner (npx/docker/…), signal (kill/pkill), outside_write (writing outside the
workspace — also frees write_file outside it). `shell deny` is a global gate.
Escalation (sudo/system, sending data off-box, deleting outside the workspace) and
the catastrophes can't be trusted away — they always ask (or are refused).

Decisions: allow | ask (`ask` clears a trust back to the default).
"""

from __future__ import annotations

from ..config.policy import load_policy, set_policy
from ..theme import console
from ..tools.shell import FREEABLE_KEYS

_DECISIONS = ("allow", "ask", "deny")
_KNOWN_KEYS = sorted(FREEABLE_KEYS) + ["shell"]  # shell = global gate


def run(args: list[str]) -> int:
    if not args:
        policy = load_policy()
        if not policy:
            console.print("No custom trust set — safe tools run, boundary crossings ask. 🌸")
        else:
            for key, decision in sorted(policy.items()):
                console.print(f"  [accent]{key}[/accent]: {decision}")
        console.print(f"[dim]Trustable: {', '.join(_KNOWN_KEYS)}[/dim]")
        return 0
    if len(args) != 2 or args[1] not in _DECISIONS:
        console.print("Usage: [accent]moko trust <class> <allow|ask|deny>[/accent]")
        console.print(f"[dim]Classes: {', '.join(_KNOWN_KEYS)}[/dim]")
        return 1
    key, decision = args
    if key not in _KNOWN_KEYS and decision == "allow":
        console.print(f"[moko]'{key}' isn't a known class[/moko] — {', '.join(_KNOWN_KEYS)}.")
        return 1
    set_policy(key, decision)
    console.print(f"[moko]Set:[/moko] {key} → {decision}.")
    return 0
