"""`moko snapshot` — back up the whole mind on demand, or `--list` what exists.

The graph is irreplaceable; this is the manual companion to the automatic
pre-migration backup. When the daemon is up it takes the snapshot (single
writer → a clean, consistent copy); otherwise the command does it in-process.
"""

from __future__ import annotations

import asyncio

from rich.panel import Panel
from rich.table import Table

from ..clock import now_utc
from ..config.paths import db_path
from ..store import backup
from ..theme import console
from . import _daemon

_KIND_LABEL = {
    "auto": "[muted]auto[/muted]",
    "manual": "manual",
    "pre-restore": "[muted]undo[/muted]",
}


def _size(n: int) -> str:
    return f"{n / 1_048_576:.1f} MB" if n >= 1_048_576 else f"{max(1, n // 1024)} KB"


def _ago(mtime: float) -> str:
    secs = max(0.0, now_utc().timestamp() - mtime)
    for unit, span in (("d", 86400), ("h", 3600), ("m", 60)):
        if secs >= span:
            return f"{int(secs // span)}{unit} ago"
    return "just now"


def _list() -> int:
    snaps = backup.list_snapshots(db_path())
    if not snaps:
        console.print("No snapshots yet. Run [accent]moko snapshot[/accent] to make one. 🌸")
        return 0
    table = Table(show_header=True, header_style="muted", box=None, padding=(0, 2, 0, 0))
    table.add_column("name")
    table.add_column("kind")
    table.add_column("size", justify="right")
    table.add_column("when", justify="right", style="muted")
    for s in snaps:
        table.add_row(f"[accent]{s.name}[/accent]", _KIND_LABEL.get(s.kind, s.kind),
                      _size(s.size), _ago(s.mtime))
    console.print(Panel(table, border_style="moko",
                        title="[accent]moko snapshots 🌸[/accent]", padding=(1, 2)))
    console.print("[muted]Restore one with [accent]moko restore <name>[/accent].[/muted]")
    return 0


def _create(label: str | None) -> int:
    # Prefer the running daemon (single writer → clean copy, shows in the monitor).
    handled = _daemon.try_command(f"snapshot {label}" if label else "snapshot")
    if handled is not None:
        return handled
    # No daemon reachable. In a container tier the daemon owns the DB; an in-process
    # copy could race its writes, so refuse and point at the sandbox.
    from ..config import config as cfg
    from ._common import refuse_if_container_daemon_down

    config = cfg.load()
    if config is not None and refuse_if_container_daemon_down(config):
        return 1
    # No daemon → snapshot in-process.
    name = backup.sanitize_label(label) if label else f"{backup.MANUAL_PREFIX}{backup.fs_stamp()}"
    try:
        target = asyncio.run(backup.snapshot_now(db_path(), name))
    except FileNotFoundError as exc:
        console.print(f"[yellow]{exc}.[/yellow]")
        return 1
    kb = max(1, target.stat().st_size // 1024)
    console.print(f"[moko]Saved a snapshot of the mind: {target.name} ({kb} KB)[/moko]")
    return 0


def run(label: str | None = None, *, list_all: bool = False) -> int:
    return _list() if list_all else _create(label)
