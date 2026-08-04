"""`moko monitor` — open the local web monitor.

The monitor server runs *inside* the always-on daemon (`moko start`), so this
command just points a browser at it. If Moko isn't running, there's nothing to
show — we say so rather than open a dead tab.
"""

from __future__ import annotations

import contextlib
import socket
import webbrowser

from ..monitor.server import DEFAULT_HOST, DEFAULT_PORT
from ..theme import console


def _is_up(host: str, port: int, timeout: float = 0.4) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(timeout)
        return sock.connect_ex((host, port)) == 0


def run(*, open_browser: bool = True) -> int:
    url = f"http://{DEFAULT_HOST}:{DEFAULT_PORT}"
    if not _is_up(DEFAULT_HOST, DEFAULT_PORT):
        console.print(
            "[moko]The monitor isn't up — it runs with Moko.[/moko] "
            "Start Moko first: [accent]moko start[/accent] "
            "(or [accent]moko service start[/accent])."
        )
        return 1
    console.print(f"[moko]Monitor:[/moko] [accent]{url}[/accent] 🌸")
    if open_browser:
        # Best-effort — headless boxes have no browser, and that's fine.
        with contextlib.suppress(Exception):
            webbrowser.open(url)
    return 0
