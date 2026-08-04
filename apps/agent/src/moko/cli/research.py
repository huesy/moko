"""`moko research` — answer one of Moko's own research questions now, on demand.

Curiosity answers itself first: a `research`-path gap is a world-fact Moko can
look up itself (vs. an `ask` gap, which only the companion can answer). Normally
this happens on idle heartbeats; this forces the next one immediately and reports
what it found — handy for testing the self-research path.

Runs directly against the shared store — fine with the daemon live (WAL +
busy_timeout serialize writes). What it learns lands in the graph (Subjects view).
"""

from __future__ import annotations

from ..agent import Brain
from ..config.paths import db_path, ensure_home
from ..store import Store
from ..theme import console
from ._common import run_via_daemon_or


async def _amain(config, client) -> int:
    ensure_home()
    store = await Store.open(db_path())
    try:
        gap = await store.next_research_gap()
        if gap is None:
            console.print(
                "[moko]No research questions queued.[/moko] These form as Moko gets "
                "curious about world-facts — chat a little, or let it reflect first."
            )
            return 0
        brain = Brain(store, client, config)
        console.print(f"[accent]Moko is looking into:[/accent] {gap.description}")
        recorded = await brain.research_gap(gap)
        console.print(
            "[moko]Found something and filed it.[/moko]" if recorded
            else "[moko]Couldn't find anything solid; set it aside for now.[/moko]"
        )
    finally:
        await store.close()
    return 0


def run() -> int:
    # The living daemon looks it up (shows in the feed); else research in-process.
    return run_via_daemon_or("research", _amain)
