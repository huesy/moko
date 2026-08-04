"""`moko status` — a warm, at-a-glance view of how Moko is set up."""

from __future__ import annotations

import socket

from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from ..clock import Clock
from ..config import config as cfg
from ..config.paths import config_path
from ..theme import console
from . import service
from .catalog import PROACTIVITY, brain_label, format_time_label


def _monitor_line(config) -> str:
    """A one-line health read of the local dashboard for `moko status`: reachable (with
    its URL), or — since the monitor is best-effort and never blocks Moko coming online —
    whether it's down because the service is stopped or because the dashboard itself
    failed to start (in which case `moko service logs` has the reason)."""
    from ..monitor import access

    port = access.MONITOR_PORT
    url = access.reachable_url(config.monitor_access, port)
    try:  # the server always listens on loopback, whatever the access tier
        with socket.create_connection(("127.0.0.1", port), timeout=0.4):
            reachable = True
    except OSError:
        reachable = False
    if reachable:
        lock = " 🔒" if access.requires_auth(config.monitor_access) else ""
        return f"[moko]running[/moko] · [accent]{url}[/accent]{lock}"
    if service.service_state().get("active"):
        return (
            "[muted]not reachable — the dashboard didn't start; "
            "see [accent]moko service logs[/accent][/muted]"
        )
    return "[muted]off — starts with Moko ([accent]moko service start[/accent])[/muted]"


def run() -> int:
    config = cfg.load()
    if config is None:
        console.print(
            Panel(
                Text.from_markup(
                    "We haven't met yet! Run [accent]moko setup[/accent] "
                    "and I'll get to know you. 🌸"
                ),
                border_style="moko",
                title="[accent]Moko[/accent]",
                padding=(1, 3),
            )
        )
        return 0

    clock = Clock(config.timezone)
    now_label = format_time_label(clock.now().strftime("%H:%M"))
    proactivity_label = dict(PROACTIVITY).get(config.proactivity, config.proactivity)

    grid = Table(show_header=False, box=None, padding=(0, 2, 0, 0))
    grid.add_column(style="muted", justify="right")
    grid.add_column()
    grid.add_row("Companion", f"[accent]{config.companion_name}[/accent]")
    grid.add_row("Right now", f"[moko]{now_label}[/moko]  ({clock.format(fmt='%A %d %B')})")
    grid.add_row("Timezone", config.timezone)
    grid.add_row(
        "Sleep",
        f"{format_time_label(config.sleep_start)} → {format_time_label(config.sleep_end)}",
    )
    grid.add_row("Presence", proactivity_label)
    grid.add_row("Brain", brain_label(config))
    grid.add_row("Service", service.status_line())
    grid.add_row("Monitor", _monitor_line(config))
    grid.add_row("Config", f"[muted]{config_path()}[/muted]")

    console.print(
        Panel(grid, border_style="moko", title="[accent]Moko 🌸[/accent]", padding=(1, 2))
    )
    return 0
