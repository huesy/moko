"""`moko restore` — replace the whole mind with a saved snapshot, safely.

Restoring is rare and destructive: it swaps Moko's entire memory. Two guards
make it safe — it refuses while the daemon is running (you can't pull the DB out
from under a live writer), and it backs up the *current* mind before overwriting
it, so a restore is itself reversible. The heavy lifting lives in `store.backup`.
"""

from __future__ import annotations

import asyncio

from ..config.paths import db_path
from ..store import backup
from ..theme import console
from . import _daemon


def _show_available() -> None:
    snaps = backup.list_snapshots(db_path())
    if not snaps:
        console.print("No snapshots yet — make one with [accent]moko snapshot[/accent].")
        return
    console.print("Available snapshots — restore one with [accent]moko restore <name>[/accent]:")
    for s in snaps:
        console.print(f"  [accent]{s.name}[/accent] [muted]({s.kind})[/muted]")


def run(name: str | None, *, assume_yes: bool = False) -> int:
    if not name:
        _show_available()
        return 1

    # Guard 1: never swap the DB out from under the running daemon.
    if _daemon.is_daemon_live():
        console.print(
            "[red]Moko is running.[/red] A restore swaps the entire mind out, which "
            "can't be done safely while it's open.\n"
            "Stop it first — [accent]moko service stop[/accent] (or quit "
            "[accent]moko start[/accent]) — then run this again."
        )
        return 1

    if asyncio.run(backup.resolve(db_path(), name)) is None:
        console.print(
            f"[red]No snapshot named {name!r}.[/red] See [accent]moko snapshot --list[/accent]."
        )
        return 1

    # Guard 2: an explicit, typed confirmation (the current mind is backed up first,
    # so this is reversible — but it's still a whole-memory replacement).
    if not assume_yes:
        console.print(
            f"This [red]replaces Moko's entire memory[/red] with snapshot "
            f"[accent]{name}[/accent].\n"
            "Your current mind is backed up first, so it's reversible."
        )
        try:
            answer = input("Type 'restore' to confirm: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            answer = ""
        if answer != "restore":
            console.print("[moko]Left everything as it was.[/moko]")
            return 0

    try:
        summary = asyncio.run(backup.restore_snapshot(db_path(), name))
    except (FileNotFoundError, ValueError, RuntimeError, OSError) as exc:
        console.print(f"[red]Restore failed:[/red] {exc}")
        return 1
    console.print(f"[moko]{summary}[/moko]")
    console.print("[muted]Start Moko again: [accent]moko service start[/accent].[/muted]")
    return 0
