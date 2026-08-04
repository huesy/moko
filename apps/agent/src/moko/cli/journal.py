"""`moko journal` — what Moko did while you weren't looking.

Shows the recent dream history (when it consolidated, what it did, what it now
wonders about) and the recent unprompted messages it sent. Read-only, so it's
safe to run while the daemon is up.
"""

from __future__ import annotations

import asyncio
import datetime as dt

from ..clock import Clock
from ..config import config as cfg
from ..config.paths import db_path
from ..store import Store
from ..theme import console


def _ago(iso: str | None, clock: Clock) -> str:
    if not iso:
        return "—"
    return clock.format(dt.datetime.fromisoformat(iso), fmt="%a %d %b %H:%M")


async def _amain(config) -> int:
    clock = Clock(config.timezone)
    store = await Store.open(db_path())
    try:
        dreams = await store.recent_dreams(limit=5)
        console.print("[bold]🌙 Dreams[/bold]")
        if not dreams:
            console.print("  [dim]No dreams yet — Moko consolidates overnight.[/dim]")
        for d in dreams:
            done = "✓" if d["ended_at"] else "… (interrupted)"
            when = _ago(d["started_at"], clock)
            console.print(f"  [accent]{when}[/accent] {done}  {d['summary']}")
            if d["wonders"]:
                console.print(f"    [dim]wonders: {d['wonders']}[/dim]")

        outreach = await store.recent_outreach(limit=10)
        console.print("\n[bold]💬 Unprompted messages[/bold]")
        if not outreach:
            console.print("  [dim]None yet.[/dim]")
        for o in outreach:
            ack = "replied" if o["engaged"] else "no reply yet"
            console.print(
                f"  [accent]{_ago(o['sent_at'], clock)}[/accent]  {o['kind']} "
                f"via {o['channel']} [dim]({ack})[/dim]"
            )
    finally:
        await store.close()
    return 0


def run() -> int:
    config = cfg.load()
    if config is None:
        console.print("We haven't met yet — run [accent]moko setup[/accent] first. 🌸")
        return 0
    return asyncio.run(_amain(config))
