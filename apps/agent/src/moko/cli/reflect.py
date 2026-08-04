"""`moko reflect` — let Moko form a private thought now, on demand.

Normally Moko reflects on its own quiet heartbeats (gated by settle + a backing-off
interval). This forces one immediately and shows what landed — handy for testing
the inner-life path. Reflection never sends; it only thinks (and may raise a
curiosity for the outreach path to surface later).

Runs directly against the shared store — fine with the daemon live (WAL +
busy_timeout serialize writes). Its result lands in the persisted Thoughts view.
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
        brain = Brain(store, client, config)
        console.print("[accent]Moko is reflecting…[/accent] 💭")
        recorded = await brain.reflect_idle(brain.clock.now(), force=True)
        if not recorded:
            console.print("[moko]A quiet mind — nothing genuinely landed this time.[/moko]")
            return 0
        latest = await store.recent_thoughts(limit=2)
        for t in latest:
            console.print(f"[moko]{t.kind}:[/moko] {t.body}")
    finally:
        await store.close()
    return 0


def run() -> int:
    # The living daemon thinks (shows in the feed); else reflect in-process.
    return run_via_daemon_or("reflect", _amain)
