"""CLI channel — chat with Moko in the terminal that runs `moko start`.

Lets you test/debug locally while Telegram (or any other channel) runs in
the same process — one gateway, one DB writer, shared memory. The local
terminal is trusted, so there's no access filter here.

Reads stdin via the event loop (no blocking thread), so Ctrl-C shuts down
cleanly.
"""

from __future__ import annotations

import asyncio
import sys

from ..theme import console
from .base import Channel, Inbound


class CliChannel(Channel):
    name = "cli"

    async def run(self, emit) -> None:
        loop = asyncio.get_running_loop()
        lines: asyncio.Queue = asyncio.Queue()

        def _on_readable() -> None:
            lines.put_nowait(sys.stdin.readline())

        loop.add_reader(sys.stdin.fileno(), _on_readable)
        try:
            while True:
                line = await lines.get()
                if line == "":  # EOF (Ctrl-D)
                    break
                text = line.strip()
                if text:
                    await emit(
                        Inbound(text=text, sender_id="local", chat_id="cli", channel=self.name)
                    )
        finally:
            loop.remove_reader(sys.stdin.fileno())

    async def send(self, text: str) -> None:
        console.print(f"[accent]moko ›[/accent] {text}")
