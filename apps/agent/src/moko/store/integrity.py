"""Structural integrity checks for the mind — `moko doctor`'s deep scan.

The config doctor answers *"is Moko set up to run?"*. These answer *"is the mind
itself sound?"* — the questions only the graph's own invariants can answer:

  • **Provenance floor** — does every fact still trace to a real source? ("every
    fact is sourced" is Moko's whole point; a fact that lost its source is a bug
    in the making.)
  • **Supersede-not-delete** — is there exactly one *current* value per fact, one
    active session, no broken history?
  • **Recall hygiene** — is the FTS index free of superseded facts (so recall
    never surfaces something Moko has marked no-longer-true)?
  • **Referential integrity** — do all the edges and links still point at real rows?

It is **report-only**: it never writes, so it's safe to run against a live daemon
(WAL lets a second reader in). A future `moko doctor --fix` can route repairs
through the daemon (the single writer). Every check is a cheap aggregate query;
the whole scan is a handful of round-trips even on a large mind.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

import aiosqlite

from ..clock import now_utc


@dataclass(frozen=True)
class Finding:
    name: str
    severity: str  # "error" (an invariant is broken) | "warn" (worth a look)
    count: int  # number of violations — 0 means the check passed
    detail: str  # human-readable summary, clean or not
    hint: str = ""  # optional: what to do about it

    @property
    def ok(self) -> bool:
        return self.count == 0


async def _scalar(conn: aiosqlite.Connection, sql: str, params: tuple = ()) -> int:
    cur = await conn.execute(sql, params)
    row = await cur.fetchone()
    return int(row[0]) if row and row[0] is not None else 0


async def _references(conn: aiosqlite.Connection) -> Finding:
    """All declared foreign keys resolve. `PRAGMA foreign_key_check` is the
    authoritative pass — it covers every source_id (provenance), subject_id (graph
    edges), and link in one go."""
    cur = await conn.execute("PRAGMA foreign_key_check")
    rows = await cur.fetchall()
    tables = sorted({r[0] for r in rows})
    detail = (
        "all references resolve"
        if not rows
        else f"{len(rows)} dangling reference(s) in: {', '.join(tables)}"
    )
    return Finding(
        "References", "error", len(rows), detail,
        hint="a row points at a missing parent — a recent snapshot may be cleaner",
    )


async def _provenance(conn: aiosqlite.Connection) -> Finding:
    """Every fact traces to a real source. source_id is NOT NULL + a foreign key, so
    this should be structurally impossible — which is exactly why it's worth
    asserting out loud: it's Moko's core promise, made into a visible, checked
    guarantee rather than an assumption."""
    n = await _scalar(
        conn,
        "SELECT (SELECT count(*) FROM attributes WHERE source_id NOT IN (SELECT id FROM sources))"
        " + (SELECT count(*) FROM observations WHERE source_id NOT IN (SELECT id FROM sources))"
        " + (SELECT count(*) FROM relationships WHERE source_id NOT IN (SELECT id FROM sources))"
        " + (SELECT count(*) FROM media WHERE source_id NOT IN (SELECT id FROM sources))",
    )
    return Finding(
        "Provenance", "error", n,
        "every fact traces to a source" if not n else f"{n} fact(s) with no resolvable source",
        hint="provenance is the whole point — inspect the write path that produced these",
    )


async def _current_attributes(conn: aiosqlite.Connection) -> Finding:
    """Exactly one *current* value per (subject, key). Two would mean a supersede was
    missed — recall could then surface a fact Moko also believes is no longer true."""
    n = await _scalar(
        conn,
        "SELECT count(*) FROM (SELECT subject_id, key FROM attributes WHERE valid_to IS NULL"
        " GROUP BY subject_id, key HAVING count(*) > 1)",
    )
    return Finding(
        "Current attributes", "error", n,
        "one current value per fact" if not n else f"{n} fact(s) with two current values",
        hint="an older copy should have been superseded (valid_to set) — a write race or merge gap",
    )


async def _current_relationships(conn: aiosqlite.Connection) -> Finding:
    """One current edge per (from, to, type) — duplicates mean a restated edge wasn't
    de-duplicated on write."""
    n = await _scalar(
        conn,
        "SELECT count(*) FROM (SELECT from_id, to_id, type FROM relationships"
        " WHERE valid_to IS NULL GROUP BY from_id, to_id, type HAVING count(*) > 1)",
    )
    return Finding(
        "Current relationships", "error", n,
        "no duplicate current edges" if not n else f"{n} duplicated current edge(s)",
        hint="add_relationship de-dups on write; duplicates suggest a bypass",
    )


async def _active_sessions(conn: aiosqlite.Connection) -> Finding:
    """At most one working-memory session is open. Extra open sessions mean a
    rotation didn't close the previous one."""
    n = await _scalar(conn, "SELECT count(*) FROM sessions WHERE ended_at IS NULL")
    extra = max(0, n - 1)
    return Finding(
        "Active session", "error", extra,
        "one active session" if extra == 0 else f"{n} sessions left open (expected ≤1)",
        hint="a session rotation didn't close the prior one; dream closes the active session",
    )


async def _stale_in_recall(conn: aiosqlite.Connection) -> Finding:
    """No superseded attribute is still in the FTS index. If one is, recall can
    surface a fact Moko has marked no-longer-true — the exact leak `_unindex` exists
    to prevent."""
    n = await _scalar(
        conn,
        "SELECT count(*) FROM search s JOIN attributes a ON a.id = s.ref_id"
        " WHERE s.kind = 'attribute' AND a.valid_to IS NOT NULL",
    )
    return Finding(
        "Recall freshness", "warn", n,
        "no stale facts in recall" if not n else f"{n} superseded fact(s) still searchable",
        hint="a supersede path skipped _unindex; `dream` reindex or a re-save clears them",
    )


async def _thought_links(conn: aiosqlite.Connection) -> Finding:
    """Thought→subject links point at real subjects. These targets are polymorphic
    (not a DB foreign key), so they need an explicit scan; a dangling one usually
    means a merge didn't re-point the inner life."""
    n = await _scalar(
        conn,
        "SELECT count(*) FROM thought_links WHERE target_kind = 'subject'"
        " AND target_id NOT IN (SELECT id FROM subjects)",
    )
    return Finding(
        "Thought links", "warn", n,
        "inner life points at real subjects" if not n else f"{n} thought(s) link a missing subject",
        hint="merge_subjects should re-point thought_links; a dangling one slipped through",
    )


async def _orphaned_dreams(conn: aiosqlite.Connection, now: dt.datetime) -> Finding:
    """No dream was left open. A dream with no `ended_at` from more than a day ago is
    a crash mid-consolidation, not the one running now."""
    cutoff = (now - dt.timedelta(days=1)).isoformat()
    n = await _scalar(
        conn,
        "SELECT count(*) FROM dreams WHERE ended_at IS NULL AND started_at < ?",
        (cutoff,),
    )
    return Finding(
        "Dream health", "warn", n,
        "no interrupted dreams" if not n else f"{n} dream(s) never finished",
        hint="a dream crashed mid-run; `close_orphaned_dreams` tidies the log",
    )


async def check(conn: aiosqlite.Connection, *, now: dt.datetime | None = None) -> list[Finding]:
    """Run every structural check against `conn` and return all findings (clean ones
    included, so the report can show the promises that hold, not just the broken
    ones). Pure reads — never writes."""
    now = now or now_utc()
    return [
        await _references(conn),
        await _provenance(conn),
        await _current_attributes(conn),
        await _current_relationships(conn),
        await _active_sessions(conn),
        await _stale_in_recall(conn),
        await _thought_links(conn),
        await _orphaned_dreams(conn, now),
    ]
