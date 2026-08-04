"""The curiosity engine — Phase A: detect knowledge gaps in the graph.

Moko's curiosity starts here: a periodic, LLM-free scan of the graph for what
it's missing or unsure about, scored by **salience** and tagged with how to
resolve it — `research` the world, `ask` the companion, or `either`. New gaps
are written to the `knowledge_gaps` queue (deduped against what's already open)
for the heartbeat to act on later: research-path gaps go to the tool layer (M7),
ask-path gaps are surfaced gently in conversation (Phase C).

Detection is deliberately **structural and conservative** — subjects we know
*of* but nothing *about*, relationships we recorded with low confidence, and
facts gone stale. It doesn't guess at what's "interesting"; the open-ended,
personal questions come from the model in conversation (the `remember` tool
already emits `open_curiosity` gaps). This fills only the gaps the graph can
prove on its own.

A gap's `description` is an **internal marker**, not the question Moko asks —
terse, deterministic (so it dedups), and in English like any log line. The
companion-facing question is generated later by the model (Phase C), naturally
and in the companion's own language; nothing here is ever surfaced verbatim.

The detectors are pure functions over in-memory graph data (easy to test);
`detect_gaps` does the I/O — read the graph, score, dedup, write.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from ..clock import now_utc
from ..log import get_logger
from ..store import COMPANION_WORLD, MOKO_WORLD, Store
from ..store.models import Attribute, Gap, Relationship, Subject

# Tunables (generic constants, never companion-specific — see CLAUDE.md).
_UNCONFIRMED_BELOW = 0.5  # a relationship recorded under this confidence is "unsure"
_STALE_AFTER_DAYS = 365  # a volatile fact older than this is worth re-checking
_STALE_RAMP_DAYS = 1095  # ...and its salience keeps rising until ~3 years old
_MAX_NEW_PER_RUN = 20  # don't flood the queue in a single scan
_LINKED_BOOST = 1.3  # gaps about the companion's own circle matter more

# Universally-volatile attribute keys: facts that tend to drift over time. Not
# tuned to any companion — just roles, places, and life status in general.
_VOLATILE_KEYS = {
    "occupation", "job", "employer", "role", "title", "residence", "address",
    "city", "location", "school", "studying", "status", "relationship_status",
}

# A thin subject is only worth chasing when it's one of the companion's *people*
# ("who is this?") — that's an `ask`. Moko already knows public places/topics
# from its own world knowledge, so it must NOT web-research their identity:
# doing so just accretes an encyclopedia in the personal graph and cascades into
# ever more subjects. (LLM-proposed gaps can still set `research` when a lookup
# genuinely helps the companion — that's the model's call, not a blanket rule.)
_ASK_KINDS = {"person", "pet"}

_log = get_logger("curiosity")


@dataclass(frozen=True)
class GapSignal:
    """A candidate gap a detector found, before it's persisted."""

    kind: str
    description: str
    about_id: str | None
    resolution_path: str
    salience: float


def _clamp(x: float) -> float:
    return max(0.0, min(1.0, x))


def _score(base: float, *, linked: bool, age_days: float | None = None) -> float:
    """Salience in [0, 1]: a base by gap type, lifted when it touches the
    companion's circle, and (for stale facts) rising with age."""
    s = base * (_LINKED_BOOST if linked else 1.0)
    if age_days is not None:
        s *= 1.0 + min(age_days / _STALE_RAMP_DAYS, 1.0)
    return round(_clamp(s), 3)


def _age_days(iso: str | None, now: dt.datetime) -> float:
    try:
        return (now - dt.datetime.fromisoformat(iso)).total_seconds() / 86400  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0.0


# ── detectors (pure) ──────────────────────────────────────────────────
def thin_subjects(
    subjects: list[Subject],
    attrs_by_id: dict[str, list[Attribute]],
    known_of: set[str],
    linked: set[str],
    *,
    skip: set[str] = frozenset(),
) -> list[GapSignal]:
    """Subjects we know *of* (referenced by a relationship) but hold no facts
    about — a name with nothing behind it."""
    out = []
    for e in subjects:
        if e.id in skip or e.id not in known_of or attrs_by_id.get(e.id):
            continue
        if e.kind not in _ASK_KINDS:
            continue  # don't chase the identity of a place/topic — Moko already knows it
        out.append(
            GapSignal(
                kind="dangling_ref",
                description=f"identity of {e.name} ({e.kind})",
                about_id=e.id,
                resolution_path="ask",  # the companion's people → ask them, never web-crawl
                salience=_score(0.55, linked=e.id in linked),
            )
        )
    return out


def unconfirmed_relationships(
    relationships: list[Relationship], names: dict[str, str], linked: set[str]
) -> list[GapSignal]:
    """Relationships we recorded but weren't sure about — worth verifying."""
    out = []
    for r in relationships:
        if r.confidence is None or r.confidence >= _UNCONFIRMED_BELOW:
            continue
        a, b = names.get(r.from_id, "?"), names.get(r.to_id, "?")
        out.append(
            GapSignal(
                kind="verification",
                description=f"verify: {a} {r.type.replace('_', ' ')} {b}",
                about_id=r.from_id,
                resolution_path="either",
                salience=_score(0.45, linked=r.from_id in linked or r.to_id in linked),
            )
        )
    return out


def stale_facts(
    attrs_by_id: dict[str, list[Attribute]],
    names: dict[str, str],
    linked: set[str],
    *,
    now: dt.datetime,
) -> list[GapSignal]:
    """Volatile facts (a job, a city) gone long enough without confirmation to
    be worth a gentle "is that still true?"."""
    out = []
    for eid, attrs in attrs_by_id.items():
        for a in attrs:
            if a.key not in _VOLATILE_KEYS:
                continue
            age = _age_days(a.valid_from or a.created_at, now)
            if age < _STALE_AFTER_DAYS:
                continue
            who = names.get(eid, "they")
            out.append(
                GapSignal(
                    kind="stale_fact",
                    description=f"recheck {who}'s {a.key.replace('_', ' ')} (was {a.value})",
                    about_id=eid,
                    resolution_path="ask",
                    salience=_score(0.4, linked=eid in linked, age_days=age),
                )
            )
    return out


_SUBJECT_SCAN_LIMIT = 5000  # full-scan bound; truncation is logged, never silent


def _linked_to_companion(relationships: list[Relationship], companion_id: str | None) -> set[str]:
    if companion_id is None:
        return set()
    return {
        (r.to_id if r.from_id == companion_id else r.from_id)
        for r in relationships
        if companion_id in (r.from_id, r.to_id)
    }


async def _persist(store: Store, signals: list[GapSignal]) -> list[Gap]:
    """Dedup signals against ALL existing gaps of any status (deterministic descriptions →
    exact match), keep the top by salience, write them. Deduping against *all* statuses (not
    just open) is what stops a resolved dangling-ref gap being re-created every turn the
    subject stays thin. Shared by both orchestrators."""
    existing = await store.gap_signatures()
    fresh = [s for s in signals if (s.kind, s.about_id, s.description) not in existing]
    fresh.sort(key=lambda s: s.salience, reverse=True)
    created = [
        await store.add_gap(
            s.description,
            s.kind,
            about_id=s.about_id,
            resolution_path=s.resolution_path,
            salience=s.salience,
        )
        for s in fresh[:_MAX_NEW_PER_RUN]
    ]
    if len(fresh) > _MAX_NEW_PER_RUN:
        _log.info("gap scan capped: kept %d of %d new gaps", _MAX_NEW_PER_RUN, len(fresh))
    return created


# ── orchestration (I/O) ───────────────────────────────────────────────
async def detect_gaps(store: Store, *, companion_id: str | None = None) -> list[Gap]:
    """FULL graph scan — for the periodic path (M5 heartbeat / dream), NOT the
    per-turn hot path: it loads the graph into memory, so cost grows with graph
    size. Turns use `detect_gaps_for` instead. Covers all detectors, including
    time-based staleness. Idempotent. Returns gaps created, highest salience
    first."""
    # Companion-facing curiosity only: never chase Moko's-own-world nodes (Moko doesn't
    # ask the companion "who is this?" about a thing in its own imagined home).
    subjects = await store.all_subjects(limit=_SUBJECT_SCAN_LIMIT, world_id=COMPANION_WORLD)
    if len(subjects) >= _SUBJECT_SCAN_LIMIT:
        _log.info(
            "full gap scan hit the %d-subject cap; the rest wait for a later pass",
            _SUBJECT_SCAN_LIMIT,
        )
    relationships = await store.all_relationships()
    attrs_by_id: dict[str, list] = {}
    for a in await store.all_attributes():
        attrs_by_id.setdefault(a.subject_id, []).append(a)
    names = {e.id: e.name for e in subjects}
    linked = _linked_to_companion(relationships, companion_id)
    known_of = {r.from_id for r in relationships} | {r.to_id for r in relationships}
    skip = {companion_id} if companion_id else set()

    return await _persist(
        store,
        [
            *thin_subjects(subjects, attrs_by_id, known_of, linked, skip=skip),
            *unconfirmed_relationships(relationships, names, linked),
            *stale_facts(attrs_by_id, names, linked, now=now_utc()),
        ],
    )


async def detect_gaps_for(
    store: Store, subject_ids: list[str], *, companion_id: str | None = None
) -> list[Gap]:
    """INCREMENTAL detection for just the subjects a turn touched — O(facts
    changed this turn), so it stays cheap no matter how large the graph grows.
    This is what the turn loop calls. Covers the change-driven gaps (thin
    subjects, unconfirmed relationships); time-based staleness is the periodic
    `detect_gaps`'s job, not a per-turn concern."""
    ids = list(dict.fromkeys(i for i in subject_ids if i))
    # Companion-facing curiosity only — skip any Moko's-world subject a turn happened to
    # touch (e.g. Moko recalling its own home); those are never an "ask the companion" gap.
    subjects = [
        e for e in [await store.get_subject(i) for i in ids]
        if e is not None and e.world_id != MOKO_WORLD
    ]
    if not subjects:
        return []
    attrs_by_id = {e.id: await store.attributes_for(e.id) for e in subjects}
    rels: list[Relationship] = []
    seen: set[str] = set()
    for e in subjects:
        for r in await store.relationships_for(e.id):
            if r.id not in seen:
                seen.add(r.id)
                rels.append(r)
    names = {e.id: e.name for e in subjects}
    missing = [nid for r in rels for nid in (r.from_id, r.to_id) if nid not in names]
    for other in await store.get_subjects(missing):  # endpoint names, one query
        names[other.id] = other.name
    linked = _linked_to_companion(rels, companion_id)
    known_of = {r.from_id for r in rels} | {r.to_id for r in rels}
    skip = {companion_id} if companion_id else set()

    return await _persist(
        store,
        [
            *thin_subjects(subjects, attrs_by_id, known_of, linked, skip=skip),
            *unconfirmed_relationships(rels, names, linked),
        ],
    )
