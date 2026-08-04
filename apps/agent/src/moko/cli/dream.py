"""`moko dream` — run a consolidation now, on demand.

Normally the daemon dreams once a night in the dream window. This forces one
immediately (handy for testing or a manual tidy) and prints what it did.

Runs directly against the shared store — fine with the daemon live; WAL +
busy_timeout serialize writes so it queues behind the daemon rather than erroring.
(A dream reads the whole graph then writes, so if the daemon writes mid-pass the
view can be a touch stale — harmless, and the next nightly dream settles it.)
"""

from __future__ import annotations

from ..agent import Brain
from ..agent.dream import dream as run_dream
from ..config.paths import db_path, ensure_home
from ..store import Store
from ..theme import console
from ._common import run_via_daemon_or


async def _amain(config, client) -> int:
    ensure_home()
    store = await Store.open(db_path())
    try:
        brain = Brain(store, client, config)
        console.print("[accent]Moko is dreaming…[/accent] 🌙")
        await run_dream(brain, brain.clock.now())
        latest = (await store.recent_dreams(limit=1))[0]
        console.print(f"[moko]Done.[/moko] {latest['summary']}")
        if latest["wonders"]:
            console.print(f"[dim]Moko now wonders: {latest['wonders']}[/dim]")
    finally:
        await store.close()
    return 0


def run() -> int:
    # Let the living daemon dream (shows in the feed); else dream in-process.
    return run_via_daemon_or("dream", _amain)
