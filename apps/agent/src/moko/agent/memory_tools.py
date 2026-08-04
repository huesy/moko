"""Moko's memory, as tools the model calls on demand.

Read tools (replace the old eager 1-hop slice):
  • recall(query)  — keyword search across subjects, facts, and messages.
  • lookup(name)   — everything known about one named thing, including its
                     relationships (so multi-hop questions just work).

Write tool (folds the old separate extraction call into the reply loop):
  • remember(...)  — record what the companion just revealed: subjects,
                     typed attributes, relationships, the speaker's
                     sentiment, a private thought, knowledge gaps. Marked
                     `terminal` so the reply isn't held for an extra
                     round-trip. The Brain binds its execution to the turn
                     (it resolves names against the companion and applies
                     the same write-time guards extraction used).

The read executor (`make_executor`) returns plain text the model reads
back; `remember`'s execution lives in the Brain, which holds the turn's
source/companion context.
"""

from __future__ import annotations

import datetime as dt

from ..clock import now_utc
from ..llm import Tool, ToolExecutor
from ..log import get_logger
from ..store import COMPANION_WORLD, MOKO_WORLD, Store


def _world_id(arg: str | None) -> str:
    """Map the tool's `world` arg ('mine' = Moko's own world) to a world id; default
    to the companion's world (the usual subject of a conversation)."""
    return MOKO_WORLD if str(arg or "").strip().lower() == "mine" else COMPANION_WORLD


def _age_phrase(iso: str | None) -> str:
    """'~3w ago' / '~2mo ago' for a fact older than a week; '' when recent (recency
    is unremarkable). Lets a lookup show *when* a belief was formed."""
    if not iso:
        return ""
    try:
        days = (now_utc() - dt.datetime.fromisoformat(iso)).days
    except ValueError:
        return ""
    if days >= 30:
        return f"~{days // 30}mo ago"
    if days >= 7:
        return f"~{days // 7}w ago"
    return ""


def _local_dt(iso: str | None, clock=None) -> str:
    """A readable date + time for a history timestamp — in the companion's timezone when a
    clock is given (the normal path), else the raw UTC date+time (tests / no clock). '' for a
    missing or malformed value."""
    if not iso:
        return ""
    try:
        when = dt.datetime.fromisoformat(iso)
    except (TypeError, ValueError):
        return ""
    return clock.format(when, "%d %b %Y, %H:%M") if clock is not None \
        else when.strftime("%d %b %Y, %H:%M")


# How a fact reached the graph, phrased so Moko hedges accordingly. The
# companion's own word (message/user_stated) is the trusted default and stays
# unmarked; anything Moko read or inferred is flagged so it doesn't get asserted
# as settled fact.
_SOURCE_PHRASE = {
    "web": "from the web", "inference": "inferred", "email": "from email",
    "calendar": "from your calendar", "file": "from a file", "vision": "from a photo",
}


def _fact_meta(attr, source_kind: str | None = None) -> str:
    """A concise '(from the web, conf 0.7, ~3w ago)' tag — shown when a fact is
    second-hand (not the companion's own word), uncertain, or stale, so Moko holds
    it as a belief to verify rather than settled truth (the honesty boundary).
    Recent, confident, companion-stated facts stay clean."""
    bits = []
    phrase = _SOURCE_PHRASE.get(source_kind or "")
    if phrase:
        bits.append(phrase)
    if attr.confidence is not None and attr.confidence < 0.85:
        bits.append(f"conf {attr.confidence:.1f}")
    age = _age_phrase(attr.valid_from or attr.created_at)
    if age:
        bits.append(age)
    return f"  ({', '.join(bits)})" if bits else ""

_log = get_logger("memory")

# Both search tools take a `world`: the companion's world (their life — the default) or
# your OWN world (your home, your things, your pets). Pick `mine` for a question about
# yourself or your world ("what's in your kitchen?", "do you have a pet?"); otherwise
# leave it — the companion's life is the usual subject.
_WORLD_PARAM = {
    "type": "string",
    "enum": ["companion", "mine"],
    "description": (
        "whose world to search: 'companion' (the person you're talking to and their life — "
        "the default) or 'mine' (your OWN world — your home, your belongings, your pets). "
        "Use 'mine' for a question about yourself or your own world."
    ),
}

RECALL_TOOL = Tool(
    name="recall",
    description=(
        "Search your memory for people, places, pets, facts, or past messages by keyword. "
        "Use it when the companion refers to something you might know. Set `world` to 'mine' "
        "if the question is about your own world rather than the companion's."
    ),
    parameters={
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "keywords to search for"},
            "world": _WORLD_PARAM,
        },
        "required": ["query"],
    },
)

LOOKUP_TOOL = Tool(
    name="lookup",
    description=(
        "Look up everything you know about one specific named person, place, pet, or thing — "
        "its details and its relationships. Use it to answer questions that need depth "
        "(e.g. where someone lives, who owns what). Set `world` to 'mine' to look something "
        "up in your own world (your home, your pet) rather than the companion's."
    ),
    parameters={
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "the exact name to look up"},
            "world": _WORLD_PARAM,
        },
        "required": ["name"],
    },
)

REFLECT_TOOL = Tool(
    name="reflect",
    description=(
        "Look inward at your own past thoughts — the opinions, feelings, hunches, and "
        "concerns you've formed over time. Pass `about` (a name or topic) to recall what "
        "you've privately thought about it, or omit it to see what's been on your mind "
        "lately. These are yours; reading them keeps your point of view and mood "
        "continuous across conversations. Whether you share any of it is still your call."
    ),
    parameters={
        "type": "object",
        "properties": {
            "about": {
                "type": "string",
                "description": "a name or topic to recall your thoughts about (optional)",
            }
        },
    },
)

MEMORY_TOOLS = [RECALL_TOOL, LOOKUP_TOOL, REFLECT_TOOL]


def _render_thoughts(thoughts) -> list[str]:
    """Render Moko's own thoughts for it to read back — kind (+ shareability when
    voiceable) + body, newest-weightiest first. The label travels with the thought
    so the honesty boundary is legible: unmarked = private (keep it). (`thoughts`
    is already ranked by the store.)"""
    out = []
    for t in thoughts:
        share = "" if t.shareability == "private" else f" · {t.shareability}"
        out.append(f"- ({t.kind}{share}) {t.body}")
    return out

# A thin functional description: the *rules* for good recording (subjects vs
# attributes, refinements, sentiment) live in the editable `remembering` skill,
# which is in the same prompt. Keeping them there, not here, is what lets the
# companion tune recording behaviour without a code change.
REMEMBER_TOOL = Tool(
    name="remember",
    description=(
        "Record what the companion just revealed about their world, in the same turn "
        "as your reply: named things (people, places, orgs, pets) as `subjects`, their "
        "scalar facts as `attributes`, point-in-time measurements or events (sleep, a "
        "weigh-in, a run) as `observations`, the links between them as `relationships`, how "
        "the companion seemed as `speaker_sentiment`, plus any private `thoughts`, "
        "knowledge `gaps`, or `commitments` you've agreed to (reminders, follow-ups, "
        "tasks). Follow your `remembering` skill for what and how to record. If you "
        "tell the companion you'll remember or remind them of something, you MUST "
        "record it here as a commitment in the same turn — otherwise you haven't. "
        "Record ONLY what is new in their most recent message: never re-record something "
        "already handled in an earlier turn — in particular, do NOT re-create a reminder "
        "that was already asked for before (it's already saved), even if it's still in view."
    ),
    terminal=True,
    parameters={
        "type": "object",
        "properties": {
            "subjects": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "kind": {
                            "type": "string",
                            "enum": ["person", "place", "org", "object", "event", "topic", "pet"],
                        },
                        "name": {"type": "string"},
                    },
                    "required": ["kind", "name"],
                },
            },
            "attributes": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "subject": {"type": "string", "description": "subject name"},
                        "key": {"type": "string", "description": "snake_case attribute key"},
                        "value": {"type": "string"},
                        "value_type": {
                            "type": "string",
                            "enum": ["string", "date", "datetime", "number", "bool", "json"],
                        },
                        "confidence": {"type": "number"},
                        "prior": {
                            "type": "boolean",
                            "description": (
                                "true if this value was true in the PAST and has already ended "
                                "('she used to be a receptionist', 'his old job'). It's recorded "
                                "as history of the SAME key, before the current value — NEVER "
                                "invent a 'previous_X'/'former_X' key or a numbered duplicate. "
                                "Omit (or false) for the current value."
                            ),
                        },
                    },
                    "required": ["subject", "key", "value"],
                },
            },
            "observations": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "subject": {
                            "type": "string",
                            "description": "subject name (usually the companion)",
                        },
                        "key": {
                            "type": "string",
                            "description": (
                                "stable snake_case series name, e.g. sleep_hours — NEVER bake a "
                                "date or 'today' into it; the date goes in observed_at"
                            ),
                        },
                        "value": {"type": "string"},
                        "value_type": {
                            "type": "string",
                            "enum": ["string", "date", "datetime", "number", "bool", "json"],
                        },
                        "observed_at": {
                            "type": "string",
                            "description": (
                                "local 'YYYY-MM-DD HH:MM' the datapoint is ABOUT (e.g. last "
                                "night) — omit for now"
                            ),
                        },
                    },
                    "required": ["subject", "key", "value"],
                },
            },
            "relationships": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "from": {"type": "string"},
                        "to": {"type": "string"},
                        "type": {"type": "string"},
                        "directed": {"type": "boolean"},
                        "confidence": {"type": "number"},
                    },
                    "required": ["from", "to", "type"],
                },
            },
            "speaker_sentiment": {
                "type": "object",
                "properties": {
                    "label": {"type": "string"},
                    "valence": {"type": "number", "description": "-1..1 (unpleasant→pleasant)"},
                    "arousal": {"type": "number", "description": "0..1 (calm→keyed-up/energised)"},
                    "confidence": {"type": "number", "description": "0..1"},
                },
                "required": ["label"],
            },
            "thoughts": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "kind": {
                            "type": "string",
                            "enum": [
                                "opinion",
                                "concern",
                                "hypothesis",
                                "prediction",
                                "curiosity",
                                "feeling",
                                "note",
                            ],
                        },
                        "body": {"type": "string"},
                        "about": {
                            "type": "string",
                            "description": (
                                "name of the person/place/thing this thought is about, if any — "
                                "so you can recall it later when that thing comes up"
                            ),
                        },
                        "valence": {
                            "type": "number",
                            "description": (
                                "how YOU feel about this, -1..1 — fond/positive above 0, "
                                "troubled/negative below 0. Set it on feelings and opinions; "
                                "omit for neutral notes. A strongly-felt thought stays with you."
                            ),
                        },
                        "shareability": {
                            "type": "string",
                            "enum": ["private", "on_ask", "shareable"],
                        },
                    },
                    "required": ["body"],
                },
            },
            "gaps": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "description": {"type": "string"},
                        "kind": {
                            "type": "string",
                            "enum": [
                                "missing_attr",
                                "unconfirmed_rel",
                                "dangling_ref",
                                "stale_fact",
                                "causal",
                                "verification",
                                "open_curiosity",
                            ],
                        },
                        "resolution_path": {
                            "type": "string",
                            "enum": ["research", "ask", "either"],
                        },
                        "about": {
                            "type": "string",
                            "description": "name of the person/place/thing it concerns",
                        },
                    },
                    "required": ["description"],
                },
            },
            "commitments": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "description": {
                            "type": "string",
                            "description": "what you've agreed to do or remind them of",
                        },
                        "kind": {"type": "string", "enum": ["reminder", "task", "followup"]},
                        "due_at": {
                            "type": "string",
                            "description": (
                                "when to surface it, as the companion's LOCAL time "
                                "'YYYY-MM-DD HH:MM' — the current date (with year) and time are "
                                "in your context, so compute it from there (in an hour, tomorrow "
                                "morning ~8am, next Friday). Omit only if there's no due time."
                            ),
                        },
                        "about": {
                            "type": "string",
                            "description": "name of the person/thing it concerns, if any",
                        },
                    },
                    "required": ["description"],
                },
            },
            "resolved_gaps": {
                "type": "array",
                "items": {"type": "integer"},
                "description": (
                    "numbers of the open questions listed in your context that the "
                    "companion just answered (or you otherwise learned) — closes them"
                ),
            },
            "saw": {
                "type": "string",
                "description": (
                    "If you were shown image(s) this turn, a brief, literal description "
                    "of what's actually visible in them — so you remember this photo "
                    "later, even after the conversation moves on. One or two sentences."
                ),
            },
        },
    },
)


async def _lookup(store: Store, name: str, world_id: str = COMPANION_WORLD, clock=None) -> str:
    subject = await store.find_by_name(name, world_id=world_id)
    if subject is None:
        return f"I have no record of anyone or anything called {name!r}."
    lines = [f"{subject.name} ({subject.kind})"]
    # Each attribute with its FULL history inline: the current value (+ provenance/confidence),
    # then each value it superseded — most-recent first, with when that value stopped being true
    # ("occupation: doctor ← nurse (until 14 Jul 2026)"). So "what was X before?" and "when did
    # it change?" are answerable at a glance. Supersede-not-delete keeps the past values; this is
    # where they reach the model.
    superseded: dict[str, list] = {}
    current_keys: set[str] = set()
    for a in await store.attributes_for(subject.id, current_only=False):
        if a.valid_to is None:
            current_keys.add(a.key)
        else:
            superseded.setdefault(a.key, []).append(a)
    for prior in superseded.values():
        prior.sort(key=lambda a: a.valid_to or "", reverse=True)

    def _chain(key: str) -> str:
        parts = [
            p.value + (f" (until {when})" if (when := _local_dt(p.valid_to, clock)) else "")
            for p in superseded.get(key, [])
        ]
        return ("  ←  " + "  ←  ".join(parts)) if parts else ""

    for attr, source_kind in await store.attributes_with_source(subject.id):
        lines.append(f"- {attr.key}: {attr.value}{_fact_meta(attr, source_kind)}{_chain(attr.key)}")
    # A key whose value was removed entirely (no current value) still carries its history.
    for key in superseded:
        if key not in current_keys:
            lines.append(f"- {key}: (no longer set){_chain(key)}")
    rels = await store.relationships_with_source(subject.id)
    # Batch the endpoint names in one query rather than one per relationship.
    others = [(r.to_id if r.from_id == subject.id else r.from_id) for r, _ in rels]
    names = {e.id: e.name for e in await store.get_subjects(others)}
    for rel, source_kind in rels:
        # Same calibrated-honesty tag as attributes: an inferred/web edge is hedged,
        # the companion's own word stays clean.
        meta = _fact_meta(rel, source_kind)
        if rel.from_id == subject.id:
            lines.append(f"- {rel.type} {names.get(rel.to_id, '?')}{meta}")
        else:
            lines.append(f"- {names.get(rel.from_id, '?')} {rel.type} {subject.name}{meta}")
    # Surface what Moko itself has felt/thought about this subject, so a lookup
    # carries inner-life continuity, not just facts. Private to Moko's context.
    # Time-anchored series (sleep, weight, runs…): the latest reading in each, so
    # the model sees what's tracked and reuses the same key. Each carries its own
    # date in observed_at, so an old reading reads as old, never as "now".
    obs = await store.latest_observations(subject.id)
    if obs:
        lines.append("Recent readings (latest in each series):")
        for o in obs:
            age = _age_phrase(o.observed_at)
            lines.append(f"- {o.key}: {o.value}{f'  ({age})' if age else ''}")
    thoughts = await store.thoughts_linked_to(subject.id)
    if thoughts:
        lines.append(f"Your private thoughts about {subject.name}:")
        lines.extend(_render_thoughts(thoughts))
    return "\n".join(lines)


async def _reflect(store: Store, about: str) -> str:
    about = about.strip()
    thoughts = (
        await store.thoughts_about(about) if about else await store.recent_thoughts(limit=6)
    )
    if not thoughts:
        if about:
            return f"You haven't formed any particular thoughts about {about} yet."
        return "Your mind's been quiet lately — no strong thoughts on record."
    header = (
        f"What you've privately thought about {about}:"
        if about
        else "What's been on your mind lately:"
    )
    return "\n".join([header, *_render_thoughts(thoughts)])


def make_executor(store: Store, clock=None) -> ToolExecutor:
    """Build the tool executor bound to a store. `clock` (the companion's timezone) lets
    `lookup` render history timestamps in local time; optional so tests can omit it."""

    async def execute(name: str, args: dict) -> str:
        _log.info("tool %s(%s)", name, args)
        if name == "recall":
            hits = await store.recall(
                str(args.get("query", "")), limit=8, world_id=_world_id(args.get("world"))
            )
            if not hits:
                return "Nothing in memory matches that."
            # Attribute/media hits are bare ("age: 7" — whose?); resolve the owning
            # subject's name at render time (never baked into FTS, so a rename/merge
            # doesn't force a reindex) and prefix it. Subject/message/thought hits
            # already carry their own text.
            owners = await store.owner_names_for(
                [h.ref_id for h in hits if h.kind == "attribute"],
                [h.ref_id for h in hits if h.kind == "media"],
            )
            lines = []
            for h in hits:
                owner = owners.get(h.ref_id) if h.kind in ("attribute", "media") else None
                content = f"{owner} — {h.content}" if owner else h.content
                lines.append(f"- ({h.kind}) {content}")
            return "\n".join(lines)
        if name == "lookup":
            return await _lookup(
                store, str(args.get("name", "")), _world_id(args.get("world")), clock=clock
            )
        if name == "reflect":
            return await _reflect(store, str(args.get("about", "")))
        return f"Unknown tool: {name}"

    return execute
