"""`moko secret` — store API credentials Moko can use, entered privately.

The secure path for giving Moko a credential: the value is typed here (hidden
input), written to ~/.moko/secrets.yaml (owner-only), and only ever read by the
shell at execution time — injected into a command that names it, redacted from
output. It never passes through the model, the chat, or the memory graph.
"""

from __future__ import annotations

import getpass

from ..config import secrets
from ..theme import console


def run(action: str = "list", name: str | None = None) -> int:
    if action == "list":
        names = secrets.secret_names()
        if not names:
            console.print("[muted]No credentials stored. Add one with[/muted] "
                          "[accent]moko secret set NAME[/accent].")
            return 0
        console.print("[moko]Stored credentials[/moko] (names only):")
        for n in names:
            console.print(f"  • {n}")
        return 0

    if action == "set":
        if not name:
            console.print("[moko]Which credential?[/moko] e.g. "
                          "[accent]moko secret set ELEVENLABS_API_KEY[/accent]")
            return 1
        try:
            value = getpass.getpass(f"Value for {name} (hidden, not echoed): ").strip()
        except (EOFError, KeyboardInterrupt):
            console.print("\n[muted]Cancelled.[/muted]")
            return 1
        if not value:
            console.print("[muted]Nothing entered — left unchanged.[/muted]")
            return 1
        secrets.set_secret(name, value)
        console.print(f"[moko]Stored '{name}'.[/moko] 🌸 Moko can use it; it never sees the value.")
        return 0

    if action == "rm":
        if not name:
            console.print("[moko]Which credential to remove?[/moko]")
            return 1
        removed = secrets.remove_secret(name)
        console.print(f"[moko]Removed '{name}'.[/moko]" if removed
                      else f"[muted]No credential named '{name}'.[/muted]")
        return 0 if removed else 1

    console.print(f"[muted]Unknown action '{action}'.[/muted]")
    return 1
