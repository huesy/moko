"""Read-only views of Moko's mind for the monitor's brain browser.

Thin serializers over the store's existing readers — turning frozen dataclass
records into JSON-able dicts. Read-only by construction: nothing here mutates
the graph. The store stays the only thing that touches SQLite; this just
shapes what it returns for the UI.
"""

from __future__ import annotations

import datetime as dt
import json
from dataclasses import asdict

from ..store import Store

# One page per fetch — the brain browser scrolls to load more (see server's
# query-param handlers). Kept modest so the first paint is cheap even when the
# mind has grown large.
PAGE = 50


async def thoughts(
    store: Store, *, limit: int = PAGE, offset: int = 0, world_id: str | None = None
) -> list[dict]:
    """The whole inner life, newest first — including notes and faded thoughts. `world_id`
    scopes to one world (a thought about the companion's life vs Moko's own)."""
    return [asdict(t) for t in await store.list_thoughts(limit=limit, offset=offset,
                                                         world_id=world_id)]


async def gaps(
    store: Store, *, limit: int = PAGE, offset: int = 0, world_id: str | None = None
) -> list[dict]:
    """All knowledge gaps (Moko's curiosity) — active ones first (open/researching/asked),
    then resolved/abandoned so completed/archived curiosities stay visible and the tab badge
    matches the rows. `world_id` scopes to one world."""
    return [asdict(g) for g in await store.list_gaps(limit=limit, offset=offset,
                                                     world_id=world_id)]


async def outreach(store: Store, *, limit: int = PAGE, offset: int = 0) -> list[dict]:
    """Moko's reaching-out history, newest first: the unprompted messages it
    *sent* AND the deliberations where it considered and (often) decided to wait —
    so staying quiet is visible as a choice with a reason, not an absence. One
    merged stream, the answer to 'has it even thought about reaching out?'."""
    span = limit + offset
    sent = [
        {"kind": "sent", "at": o.get("sent_at"), "channel": o.get("channel"),
         "outreach_kind": o.get("kind"), "engaged": o.get("engaged")}
        for o in await store.recent_outreach(limit=span)
    ]
    consults = [
        {"kind": "consult", "at": c.get("at"), "acted": bool(c.get("acted")),
         "candidate": c.get("candidate"), "reasoning": c.get("reasoning"),
         "reconsider_minutes": c.get("reconsider_minutes")}
        for c in await store.recent_outreach_decisions(limit=span)
    ]
    merged = sorted(sent + consults, key=lambda e: e.get("at") or "", reverse=True)
    return merged[offset:span]


async def commitments(store: Store, *, limit: int = PAGE, offset: int = 0) -> list[dict]:
    """All commitments, newest first — open *and* done, so the browser shows the
    full history, not only what's still pending."""
    return [asdict(c) for c in await store.list_commitments(limit=limit, offset=offset)]


async def messages(store: Store, *, limit: int = PAGE, offset: int = 0) -> list[dict]:
    """Recent messages across channels, newest first (paged history)."""
    return [asdict(m) for m in await store.list_messages(limit=limit, offset=offset)]


async def subjects(
    store: Store, *, limit: int = PAGE, offset: int = 0, world_id: str | None = None
) -> list[dict]:
    """Every subject in the graph (excluding Moko's own self node). `world_id` filters to
    one world (the companion's vs Moko's); None shows both. Each row carries its `world_id`."""
    return [
        asdict(e)
        for e in await store.all_subjects(limit=limit, offset=offset, world_id=world_id)
    ]


async def media(
    store: Store, *, limit: int = PAGE, offset: int = 0, world_id: str | None = None
) -> list[dict]:
    """Recent media artifacts (newest first) — the metadata the monitor's gallery shows; the
    bytes themselves are fetched per-item from the blob endpoint by id. `world_id` scopes the
    list to one world, server-side."""
    return [
        {"id": m.id, "kind": m.kind, "mime": m.mime, "direction": m.direction,
         "caption": m.caption, "description": m.description, "filename": m.filename,
         "created_at": m.created_at, "world_id": m.world_id}
        for m in await store.recent_media(limit=limit, offset=offset, world_id=world_id)
    ]




def _dream_duration(started_at: str | None, ended_at: str | None) -> float | None:
    """Seconds the dream ran, or None if it didn't finish / can't be parsed."""
    if not started_at or not ended_at:
        return None
    try:
        started = dt.datetime.fromisoformat(started_at)
        ended = dt.datetime.fromisoformat(ended_at)
    except (ValueError, TypeError):
        return None
    return (ended - started).total_seconds()


async def dreams(store: Store, *, limit: int = PAGE, offset: int = 0) -> list[dict]:
    """The dream journal — each nightly consolidation, newest first. Parses the
    stored stats JSON into a dict and adds a duration, so the detail view can show
    what happened (the counts), Moko's private `wonders`, and how long it took."""
    out: list[dict] = []
    for d in await store.recent_dreams(limit=limit, offset=offset):
        raw = d.get("stats")
        try:
            stats = json.loads(raw) if isinstance(raw, str) and raw else (raw or {})
        except json.JSONDecodeError:
            stats = {}
        out.append(
            {
                "id": d.get("id"),
                "started_at": d.get("started_at"),
                "ended_at": d.get("ended_at"),
                "summary": d.get("summary") or "",
                "wonders": d.get("wonders") or "",
                "stats": stats if isinstance(stats, dict) else {},
                "duration_seconds": _dream_duration(d.get("started_at"), d.get("ended_at")),
            }
        )
    return out


async def subject_detail(store: Store, subject_id: str) -> dict | None:
    """One subject with its attributes, relationships (with the other party's
    name resolved), and Moko's thoughts about it. None if no such subject."""
    subject = await store.get_subject(subject_id)
    if subject is None:
        return None

    attributes = await store.attributes_for(subject_id)
    rels = await store.relationships_for(subject_id)

    # Resolve the names on the far end of each relationship in one round-trip.
    other_ids = {r.from_id for r in rels} | {r.to_id for r in rels}
    other_ids.discard(subject_id)
    names = {e.id: e.name for e in await store.get_subjects(list(other_ids))}

    relationships = []
    for r in rels:
        outgoing = r.from_id == subject_id
        other = r.to_id if outgoing else r.from_id
        relationships.append(
            {
                "id": r.id,
                "type": r.type,
                "direction": "out" if outgoing else "in",
                "other_id": other,
                "other_name": names.get(other, other),
                "valid_from": r.valid_from,
                "valid_to": r.valid_to,
            }
        )

    linked = await store.thoughts_linked_to(subject_id, limit=20)
    curiosities = await store.gaps_about(subject_id)
    return {
        "subject": asdict(subject),
        "attributes": [asdict(a) for a in attributes],
        "relationships": relationships,
        "thoughts": [asdict(t) for t in linked],
        "gaps": [asdict(g) for g in curiosities],
    }


def _subject_ref(subject) -> dict:
    """A compact, clickable subject reference for a detail view — just enough to
    label it and navigate to its full card."""
    return {"id": subject.id, "name": subject.name, "kind": subject.kind}


async def thought_detail(store: Store, thought_id: str) -> dict | None:
    """One thought with the subjects it's about and the gaps (curiosities) about
    those same subjects — so a thought and a same-subject question are navigable to
    each other, the way a curiosity 'I wonder what Miyu is to Andrew' sits next to
    the gap about Miyu. None if no such thought."""
    thought = await store.get_thought(thought_id)
    if thought is None:
        return None
    subjects = await store.linked_subjects_for_thought(thought_id)
    # The gaps about any subject of this thought, deduped (a thought can be about
    # more than one subject; the same gap shouldn't appear twice).
    seen: set[str] = set()
    related_gaps: list[dict] = []
    for subject in subjects:
        for gap in await store.gaps_about(subject.id):
            if gap.id not in seen:
                seen.add(gap.id)
                related_gaps.append(asdict(gap))
    return {
        "thought": asdict(thought),
        "subjects": [_subject_ref(e) for e in subjects],
        "gaps": related_gaps,
    }


async def gap_detail(store: Store, gap_id: str) -> dict | None:
    """One gap with its subject subject and Moko's thoughts about that subject — the
    inverse of `thought_detail`, so clicking a curiosity surfaces the thoughts it
    grew from or sits beside. None if no such gap."""
    gap = await store.get_gap(gap_id)
    if gap is None:
        return None
    subject = await store.get_subject(gap.about_id) if gap.about_id else None
    thoughts = await store.thoughts_linked_to(gap.about_id, limit=20) if gap.about_id else []
    return {
        "gap": asdict(gap),
        "subject": _subject_ref(subject) if subject else None,
        "thoughts": [asdict(t) for t in thoughts],
    }
