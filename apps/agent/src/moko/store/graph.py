"""The graph store — Moko's memory, in one cohesive API.

Hard rules from CLAUDE.md, enforced here:
  • every attribute and relationship carries a source (provenance) and a
    timestamp — `set_attribute`/`add_relationship` require a source_id;
  • facts are superseded, never deleted (`valid_to`), so history stays;
  • subject resolution is conservative at write time — strong matches
    merge, weak ones become `merge_candidates` for the dream process.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field

import aiosqlite

from ..clock import now_utc, utc_isoformat
from ..events import publish as _publish_event
from . import blobs as media_blobs
from .db import _pylower, connect
from .models import (
    Attribute,
    Commitment,
    Gap,
    MediaArtifact,
    Message,
    Observation,
    Relationship,
    SearchHit,
    Session,
    Source,
    Subject,
    Thought,
    new_id,
)
from .usage import UsageLedger

# The two well-known singleton worlds (see migration 0018). Subjects default to the
# companion's world; Moko's own home/world subjects (its self-node, the places it
# inhabits, things it sees in its generated pictures) are written to MOKO_WORLD.
COMPANION_WORLD = "world-companion"
MOKO_WORLD = "world-moko"


@dataclass
class GraphSlice:
    """The relevant sub-graph for a context: seeds + one hop + recents."""

    subjects: list[Subject] = field(default_factory=list)
    relationships: list[Relationship] = field(default_factory=list)
    attributes: dict[str, list[Attribute]] = field(default_factory=dict)
    messages: list[Message] = field(default_factory=list)


_VALUE_TYPES = {"string", "date", "datetime", "number", "bool", "json"}


def _validated_value_type(value: str, value_type: str) -> str:
    """Return `value_type` only if `value` actually parses as it; otherwise
    `string`. Keeps the typed-values invariant true without losing data — a model
    that mislabels "around thirty" as a `number` gets it stored as the string it
    is, so a later reader doing `float()`/`json.loads()`/date-parse can trust the
    type. (Validation belongs at the single write seam, not the caller.)"""
    vt = (value_type or "string").strip().lower()
    if vt not in _VALUE_TYPES or vt == "string":
        return "string"
    try:
        if vt == "number":
            float(value)
        elif vt == "bool":
            if value.strip().lower() not in {"true", "false", "0", "1", "yes", "no"}:
                return "string"
        elif vt == "json":
            json.loads(value)
        elif vt in {"date", "datetime"}:
            dt.datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return "string"
    return vt


class Store:
    """Async wrapper over the SQLite mind."""

    def __init__(self, conn: aiosqlite.Connection) -> None:
        self.conn = conn
        self.usage = UsageLedger(conn)  # token ledger + per-call I/O (no graph coupling)

    @classmethod
    async def open(cls, path) -> Store:
        return cls(await connect(str(path)))

    @classmethod
    async def open_readonly(cls, path) -> Store:
        """A read-only handle on an existing mind — for observers (the monitor) that
        must never write. Opened AFTER the main Store (so the DB + its WAL files
        already exist); it therefore skips migration, the chmod, and any journal_mode
        PRAGMA (all write acts). Same row_factory + busy_timeout as `open` so reads
        behave identically and queue behind the writer instead of failing on a lock."""
        conn = await aiosqlite.connect(f"file:{path}?mode=ro", uri=True)
        conn.row_factory = aiosqlite.Row
        # Mirror the write handle so dedup-dependent read queries stay Unicode-correct.
        await conn.create_function("pylower", 1, _pylower, deterministic=True)
        await conn.execute("PRAGMA busy_timeout = 5000")
        return cls(conn)

    async def close(self) -> None:
        await self.conn.close()

    # ── full-text index (app-maintained) ─────────────────────────────
    async def _index(self, kind: str, ref_id: str, content: str) -> None:
        await self.conn.execute(
            "INSERT INTO search (content, kind, ref_id) VALUES (?, ?, ?)",
            (content, kind, ref_id),
        )

    async def _unindex(self, ref_id: str) -> None:
        """Drop a row's FTS entry — called when the underlying fact is superseded
        or folded, so recall never surfaces a value Moko has marked no-longer-true.
        The app maintains the index by hand (no triggers), so every supersede path
        must call this or stale facts leak straight to the model."""
        await self.conn.execute("DELETE FROM search WHERE ref_id = ?", (ref_id,))

    async def _reindex_subject(self, subject_id: str) -> None:
        """Rebuild a subject's FTS rows from its canonical name + every current alias.
        A subject's searchable text is one `search` row per name/alias, all keyed by the
        subject id; a merge changes that set (the survivor absorbs the folded node's name
        and aliases), so after a fold the survivor must be re-indexed — otherwise it's
        unrecallable by the duplicate's names, defeating the point of keeping them as
        aliases. No commit (callers batch it into their own transaction)."""
        cur = await self.conn.execute("SELECT name FROM subjects WHERE id = ?", (subject_id,))
        row = await cur.fetchone()
        if row is None:
            return
        await self._unindex(subject_id)
        await self._index("subject", subject_id, row["name"])
        cur = await self.conn.execute(
            "SELECT alias FROM subject_aliases WHERE subject_id = ?", (subject_id,)
        )
        for arow in await cur.fetchall():
            await self._index("subject", subject_id, arow["alias"])

    async def optimize_search(self) -> None:
        """Compact the FTS index (dream upkeep) — merges segments for faster
        recall. Safe and non-destructive."""
        await self.conn.execute("INSERT INTO search (search) VALUES ('optimize')")
        await self.conn.commit()

    async def heal_search(self) -> int:
        """Nightly self-heal: DELETE stale `search` rows so recall never surfaces a
        fact Moko has marked no-longer-true. The index is app-maintained (no triggers),
        so a supersede/merge path that skips `_unindex` leaks a row — exactly the class
        `integrity._stale_in_recall` monitors. Two leak sources: an attribute search row
        whose attribute has been superseded (`attributes.valid_to IS NOT NULL`), and a
        subject search row whose subject was merged (`subjects.merged_into IS NOT NULL`).
        Returns rows healed. Dream calls it next to `optimize_search`."""
        cur = await self.conn.execute(
            "DELETE FROM search WHERE ref_id IN ("
            " SELECT s.ref_id FROM search s JOIN attributes a ON a.id = s.ref_id"
            "  WHERE s.kind = 'attribute' AND a.valid_to IS NOT NULL"
            " UNION"
            " SELECT s.ref_id FROM search s JOIN subjects e ON e.id = s.ref_id"
            "  WHERE s.kind = 'subject' AND e.merged_into IS NOT NULL)"
        )
        await self.conn.commit()
        return cur.rowcount

    def _fts_match(self, query: str) -> str | None:
        """An FTS5 MATCH string from free text: significant terms (>2 chars) OR-ed and
        quoted to escape FTS operators. None when nothing usable remains. Single-sourced
        so subject recall and thought recall can't drift in how they parse a query."""
        terms = [w for w in re.findall(r"[A-Za-z0-9]+", query) if len(w) > 2]
        if not terms:
            return None
        return " OR ".join(f'"{term}"' for term in terms)

    async def recall(
        self, query: str, limit: int = 10, *, world_id: str | None = None
    ) -> list[SearchHit]:
        """Find subjects/attributes/messages/thoughts by their text.

        Matches on any significant term (OR), not the whole phrase — so a
        natural-language question still surfaces the subjects it mentions.

        `world_id` scopes subject/attribute/media hits to one world (so a search of
        the companion's world never surfaces Moko's own furniture or a selfie's
        description, and vice versa); message/thought hits aren't world-bound.
        """
        match = self._fts_match(query)
        if match is None:
            return []
        # Over-fetch when filtering by world, so the post-filter can still fill `limit`.
        fetch = limit if world_id is None else limit * 4
        # ORDER BY bm25(search): FTS5 returns rowid (insertion) order by default, so
        # without this a common term surfaces the *oldest* matches as the mind grows,
        # not the best. bm25 scores more-negative = more-relevant, so ascending is
        # best-first. (`search` is an fts5 virtual table — see migration 0001 — so
        # bm25() is a valid ranking function here.) The world over-fetch stands: it now
        # filters the best-ranked hits, keeping relevance after the world post-filter.
        cur = await self.conn.execute(
            "SELECT kind, ref_id, content FROM search WHERE search MATCH ?"
            " ORDER BY bm25(search) LIMIT ?",
            (match, fetch),
        )
        hits = [SearchHit(r["kind"], r["ref_id"], r["content"]) for r in await cur.fetchall()]
        if world_id is None:
            return hits[:limit]
        return (await self._filter_hits_by_world(hits, world_id))[:limit]

    async def _filter_hits_by_world(
        self, hits: list[SearchHit], world_id: str
    ) -> list[SearchHit]:
        """Drop subject/attribute/media hits that belong to a different world; keep the rest
        (messages/thoughts aren't world-bound). Batched lookups, not N+1. Every media row
        carries a world_id (selfies/world photos → Moko's, everything else → the companion's),
        so a world-scoped recall never leaks one world's media description into the other."""
        subj_ids = [h.ref_id for h in hits if h.kind == "subject"]
        attr_ids = [h.ref_id for h in hits if h.kind == "attribute"]
        media_ids = [h.ref_id for h in hits if h.kind == "media"]
        ok_subj: set[str] = set()
        if subj_ids:
            q = ",".join("?" * len(subj_ids))
            cur = await self.conn.execute(
                f"SELECT id FROM subjects WHERE id IN ({q}) AND world_id = ?",
                (*subj_ids, world_id),
            )
            ok_subj = {r["id"] for r in await cur.fetchall()}
        ok_attr: set[str] = set()
        if attr_ids:
            q = ",".join("?" * len(attr_ids))
            cur = await self.conn.execute(
                f"SELECT a.id FROM attributes a JOIN subjects s ON s.id = a.subject_id"
                f" WHERE a.id IN ({q}) AND s.world_id = ?",
                (*attr_ids, world_id),
            )
            ok_attr = {r["id"] for r in await cur.fetchall()}
        ok_media: set[str] = set()
        if media_ids:
            q = ",".join("?" * len(media_ids))
            cur = await self.conn.execute(
                f"SELECT id FROM media WHERE id IN ({q}) AND world_id = ?",
                (*media_ids, world_id),
            )
            ok_media = {r["id"] for r in await cur.fetchall()}
        keep = []
        for h in hits:
            if h.kind == "subject" and h.ref_id not in ok_subj:
                continue
            if h.kind == "attribute" and h.ref_id not in ok_attr:
                continue
            if h.kind == "media" and h.ref_id not in ok_media:
                continue
            keep.append(h)
        return keep

    async def owner_names_for(
        self, attribute_ids: list[str], media_ids: list[str]
    ) -> dict[str, str]:
        """Map attribute-ids and media-ids to their owning subject's name, in one query
        each. Recall renders an attribute hit's content as a bare "key: value" ("age: 7")
        and a media hit's as a bare description — whose? This resolves the owner at
        *render* time rather than baking the name into the FTS content, so a rename or
        merge doesn't force a reindex. An attribute's owner is its `subject_id`; a media
        artifact has no `subject_id`, so its owner is the speaker of the message it rode
        in on (absent for a generated/outbound artifact with no message or speaker).
        Returned dict is keyed by ref_id (attribute or media id); ids with no live owner
        are simply absent, so the caller falls back to the bare content."""
        names: dict[str, str] = {}
        attr_ids = list(dict.fromkeys(attribute_ids))
        if attr_ids:
            q = ",".join("?" * len(attr_ids))
            cur = await self.conn.execute(
                f"SELECT a.id, s.name FROM attributes a JOIN subjects s ON s.id = a.subject_id"
                f" WHERE a.id IN ({q})",
                attr_ids,
            )
            names.update({r["id"]: r["name"] for r in await cur.fetchall()})
        mids = list(dict.fromkeys(media_ids))
        if mids:
            q = ",".join("?" * len(mids))
            cur = await self.conn.execute(
                f"SELECT m.id, s.name FROM media m"
                f" JOIN messages msg ON msg.id = m.message_id"
                f" JOIN subjects s ON s.id = msg.speaker_id WHERE m.id IN ({q})",
                mids,
            )
            names.update({r["id"]: r["name"] for r in await cur.fetchall()})
        return names

    # ── sources (provenance) ──────────────────────────────────────────
    async def add_source(
        self,
        kind: str,
        ref: str = "",
        *,
        channel: str | None = None,
        excerpt: str | None = None,
        observed_at: str | None = None,
    ) -> Source:
        now = utc_isoformat()
        sid = new_id()
        observed = observed_at or now
        await self.conn.execute(
            "INSERT INTO sources (id, kind, ref, channel, excerpt, observed_at, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (sid, kind, ref, channel, excerpt, observed, now),
        )
        await self.conn.commit()
        return Source(sid, kind, ref, channel, excerpt, observed, now)

    # ── subjects + resolution ─────────────────────────────────────────
    async def add_subject(
        self, kind: str, name: str, *, confidence: float = 1.0, notes: str = "",
        world_id: str | None = None, dedupe: bool = True,
    ) -> Subject:
        """Create a subject, or return the strong match if one exists.

        `world_id` says whose world the subject is part of (defaults to the companion's);
        Moko's own world is passed explicitly. Resolution + merge-candidate flagging are
        scoped to that world, so "the companion's kitchen" and "Moko's kitchen" stay
        distinct nodes that never merge.

        `dedupe=False` skips the by-name resolution AND the merge-candidate flagging — for
        generic world objects ("armchair", "rug") whose identity is *within a place*, not the
        whole world (the world model resolves those per-place before calling, so two rooms can
        each own an "armchair" without merging into one node or flooding the dream).

        Weak matches (same name, different kind, *same world*) don't merge — they're
        recorded as merge_candidates for the dream process to judge.
        """
        world = world_id or COMPANION_WORLD
        if dedupe:
            existing = await self._strong_match(kind, name, world)
            if existing is not None:
                return existing

        now = utc_isoformat()
        eid = new_id()
        await self.conn.execute(
            "INSERT INTO subjects (id, kind, name, confidence, notes, created_at, world_id)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (eid, kind, name, confidence, notes, now, world),
        )
        await self._index("subject", eid, name)
        if dedupe:
            await self._flag_name_candidates(eid, kind, name, world)
        await self.conn.commit()
        _publish_event({"type": "learned", "what": "subject", "id": eid, "kind": kind,
                        "name": name, "world_id": world}, backfill=False)
        return Subject(eid, kind, name, confidence, notes, now, world_id=world)

    async def objects_in_place(self, place_id: str) -> list[Subject]:
        """The subjects currently `located_in` a place — the things the world model has
        already put in this room. Used to ground re-analysis (reuse 'the armchair' instead
        of forking a new one) and to resolve an object per-place rather than per-world."""
        cur = await self.conn.execute(
            "SELECT s.* FROM subjects s JOIN relationships r ON r.from_id = s.id"
            " WHERE r.type = 'located_in' AND r.to_id = ? AND r.valid_to IS NULL"
            " AND s.merged_into IS NULL",
            (place_id,),
        )
        return [Subject.from_row(r) for r in await cur.fetchall()]

    async def _strong_match(self, kind: str, name: str, world_id: str) -> Subject | None:
        # pylower (Unicode casefold) on both sides — SQL's lower() is ASCII-only, so
        # without it "Müller"/"MÜLLER" wouldn't match and would mint a duplicate.
        norm = name.strip().casefold()
        cur = await self.conn.execute(
            "SELECT * FROM subjects WHERE kind = ? AND pylower(name) = ? AND world_id = ?"
            " AND merged_into IS NULL",
            (kind, norm, world_id),
        )
        row = await cur.fetchone()
        if row is not None:
            return Subject.from_row(row)
        cur = await self.conn.execute(
            "SELECT e.* FROM subject_aliases a JOIN subjects e ON e.id = a.subject_id"
            " WHERE pylower(a.alias) = ? AND e.kind = ? AND e.world_id = ?",
            (norm, kind, world_id),
        )
        row = await cur.fetchone()
        return Subject.from_row(row) if row is not None else None

    async def _flag_name_candidates(self, eid: str, kind: str, name: str, world_id: str) -> None:
        # Only flag a same-name/different-kind clash *within the same world* — a same name
        # across worlds (Moko's "kitchen" vs the companion's) is genuinely a different thing.
        cur = await self.conn.execute(
            "SELECT id FROM subjects WHERE pylower(name) = ? AND kind != ? AND world_id = ?"
            " AND id != ?",
            (name.strip().casefold(), kind, world_id, eid),
        )
        now = utc_isoformat()
        for row in await cur.fetchall():
            await self.conn.execute(
                "INSERT INTO merge_candidates (id, a_id, b_id, score, evidence, status, created_at)"
                " VALUES (?, ?, ?, ?, ?, 'open', ?)",
                (new_id(), eid, row["id"], 0.5, "same name, different kind", now),
            )

    async def add_alias(self, subject_id: str, alias: str, *, source_id: str | None = None) -> None:
        await self.conn.execute(
            "INSERT INTO subject_aliases (id, subject_id, alias, source_id, created_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (new_id(), subject_id, alias, source_id, utc_isoformat()),
        )
        await self._index("subject", subject_id, alias)
        await self.conn.commit()

    async def get_subjects(self, ids: list[str]) -> list[Subject]:
        """Fetch many subjects in one query — batches the N+1 that a per-id loop
        would otherwise make (used by slice and incremental gap detection)."""
        unique = list(dict.fromkeys(ids))
        if not unique:
            return []
        placeholders = ",".join("?" * len(unique))
        cur = await self.conn.execute(
            f"SELECT * FROM subjects WHERE id IN ({placeholders})", unique
        )
        return [Subject.from_row(r) for r in await cur.fetchall()]

    async def get_subject(self, subject_id: str) -> Subject | None:
        cur = await self.conn.execute("SELECT * FROM subjects WHERE id = ?", (subject_id,))
        row = await cur.fetchone()
        return Subject.from_row(row) if row is not None else None

    async def resolve(
        self, kind: str, name: str, *, world_id: str = COMPANION_WORLD
    ) -> Subject | None:
        """Find an existing subject by exact name/alias in a world (no creation)."""
        return await self._strong_match(kind, name, world_id)

    async def find_by_name(
        self, name: str, *, world_id: str | None = None, kind: str | None = None
    ) -> Subject | None:
        """Find a subject by name (best-effort, one row). `world_id` scopes to one world (so
        'the kitchen' resolves in the right world); `kind` scopes to one kind (so a same-named
        object can't shadow the place you're after). None for either = unconstrained."""
        clauses = ["lower(name) = ?", "merged_into IS NULL"]
        params: list = [name.strip().lower()]
        if world_id is not None:
            clauses.append("world_id = ?")
            params.append(world_id)
        if kind is not None:
            clauses.append("kind = ?")
            params.append(kind)
        cur = await self.conn.execute(
            f"SELECT * FROM subjects WHERE {' AND '.join(clauses)} LIMIT 1", params
        )
        row = await cur.fetchone()
        return Subject.from_row(row) if row is not None else None

    # ── attributes (typed, sourced, time-aware) ───────────────────────
    async def set_attribute(
        self,
        subject_id: str,
        key: str,
        value: str,
        *,
        source_id: str,
        value_type: str = "string",
        confidence: float = 1.0,
        valid_from: str | None = None,
    ) -> Attribute:
        """Set an attribute, superseding (not deleting) any current value."""
        if not source_id:
            raise ValueError("attributes require a source_id (provenance is mandatory)")
        # Typed values are validated on write: a declared type that the value
        # doesn't actually parse as is corrected to `string` (data kept, type honest).
        value_type = _validated_value_type(value, value_type)
        # If the current value is unchanged, leave it be — restating a fact
        # shouldn't churn history into superseded duplicates.
        cur = await self.conn.execute(
            "SELECT * FROM attributes WHERE subject_id = ? AND key = ? AND valid_to IS NULL",
            (subject_id, key),
        )
        current = await cur.fetchone()
        if current is not None and current["value"] == value:
            return Attribute.from_row(current)
        now = utc_isoformat()
        await self.conn.execute(
            "UPDATE attributes SET valid_to = ?"
            " WHERE subject_id = ? AND key = ? AND valid_to IS NULL",
            (now, subject_id, key),
        )
        if current is not None:
            await self._unindex(current["id"])  # the old value is no longer current — un-recall it
        aid = new_id()
        await self.conn.execute(
            "INSERT INTO attributes"
            " (id, subject_id, key, value, value_type, valid_from, valid_to, source_id,"
            "  confidence, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, NULL, ?, ?, ?)",
            (aid, subject_id, key, value, value_type, valid_from or now, source_id,
             confidence, now),
        )
        await self._index("attribute", aid, f"{key}: {value}")
        await self.conn.commit()
        _publish_event({"type": "learned", "what": "fact", "subject_id": subject_id,
                        "key": key, "value": value}, backfill=False)
        return Attribute(
            aid,
            subject_id,
            key,
            value,
            value_type,
            valid_from or now,
            None,
            source_id,
            confidence,
            now,
        )

    async def add_past_attribute(
        self, subject_id: str, key: str, value: str, *,
        source_id: str, value_type: str = "string", confidence: float = 1.0,
    ) -> None:
        """Record a value that was true in the PAST and has already ENDED — "she used to be a
        receptionist" when her occupation is now doctor — as history of the SAME key. It slots
        into the key's timeline *before* the currently-earliest value (its `valid_to` is when
        that value began; its own start is unknown → NULL), and it never disturbs the current
        value, so the timeline reads receptionist → nurse → doctor.

        Idempotent: a value already on record for this key (current OR historical) is not
        re-added — this is the fundamental fix for a lack of a past-value primitive, which forced
        the model to invent parallel keys (`previous_occupation`, `previous_occupation_2`).
        Not recall-indexed: like any superseded value, it lives in the lookup history, not the
        current-fact search."""
        if not source_id:
            raise ValueError("attributes require a source_id (provenance is mandatory)")
        value_type = _validated_value_type(value, value_type)
        cur = await self.conn.execute(
            "SELECT 1 FROM attributes WHERE subject_id = ? AND key = ? AND value = ? LIMIT 1",
            (subject_id, key, value),
        )
        if await cur.fetchone() is not None:
            return  # already known (current or historical) — don't duplicate
        cur = await self.conn.execute(
            "SELECT MIN(valid_from) AS m FROM attributes"
            " WHERE subject_id = ? AND key = ? AND valid_from IS NOT NULL",
            (subject_id, key),
        )
        row = await cur.fetchone()
        now = utc_isoformat()
        valid_to = row["m"] if row and row["m"] else now  # ended when the next value began
        aid = new_id()
        await self.conn.execute(
            "INSERT INTO attributes"
            " (id, subject_id, key, value, value_type, valid_from, valid_to, source_id,"
            "  confidence, created_at)"
            " VALUES (?, ?, ?, ?, ?, NULL, ?, ?, ?, ?)",
            (aid, subject_id, key, value, value_type, valid_to, source_id, confidence, now),
        )
        await self.conn.commit()
        _publish_event({"type": "learned", "what": "past_fact", "subject_id": subject_id,
                        "key": key, "value": value}, backfill=False)

    async def recent_attribute_changes(self, since: str) -> list[dict]:
        """Attributes whose value changed since `since` (a superseded value that
        has a current replacement) — the day's value changes, for the dream to
        judge contradiction vs. refinement."""
        cur = await self.conn.execute(
            "SELECT s.subject_id, e.name, s.key, s.value AS old_value, c.value AS new_value"
            " FROM attributes s"
            " JOIN attributes c ON c.subject_id=s.subject_id AND c.key=s.key AND c.valid_to IS NULL"
            " JOIN subjects e ON e.id=s.subject_id"
            " WHERE s.valid_to IS NOT NULL AND s.valid_to >= ?",
            (since,),
        )
        return [dict(r) for r in await cur.fetchall()]

    async def end_attribute(self, attribute_id: str, *, valid_to: str | None = None) -> None:
        """Supersede an attribute (set `valid_to`) — kept in history, never
        deleted. Used when the dream reclassifies a misfiled value into an edge."""
        await self.conn.execute(
            "UPDATE attributes SET valid_to = ? WHERE id = ? AND valid_to IS NULL",
            (valid_to or utc_isoformat(), attribute_id),
        )
        await self._unindex(attribute_id)  # superseded → drop it from recall
        await self.conn.commit()

    async def attributes_for(
        self, subject_id: str, *, current_only: bool = True
    ) -> list[Attribute]:
        sql = "SELECT * FROM attributes WHERE subject_id = ?"
        if current_only:
            sql += " AND valid_to IS NULL"
        sql += " ORDER BY created_at"
        cur = await self.conn.execute(sql, (subject_id,))
        return [Attribute.from_row(r) for r in await cur.fetchall()]

    async def attributes_with_source(self, subject_id: str) -> list[tuple[Attribute, str]]:
        """Current attributes paired with their source KIND (message/user_stated/
        web/inference/email/…). Lets a reader show provenance so Moko hedges a
        web-read or inferred fact ("I read that…") instead of asserting it like the
        companion's own word — calibrated honesty, grounded in the graph."""
        cur = await self.conn.execute(
            "SELECT a.*, s.kind AS source_kind FROM attributes a"
            " JOIN sources s ON s.id = a.source_id"
            " WHERE a.subject_id = ? AND a.valid_to IS NULL ORDER BY a.created_at",
            (subject_id,),
        )
        return [(Attribute.from_row(r), r["source_kind"]) for r in await cur.fetchall()]

    # ── observations (time-anchored datapoints — the append-only series) ──
    async def add_observation(
        self,
        subject_id: str,
        key: str,
        value: str,
        *,
        source_id: str,
        value_type: str = "string",
        observed_at: str | None = None,
    ) -> Observation:
        """Record one datapoint in a series. Unlike an attribute, an observation
        never supersedes — each reading stays true at its instant forever, so the
        series accumulates. Restating the same (subject, key, observed_at) updates
        it in place rather than double-counting. No FTS row: a series is read by
        (subject, key), not surfaced through free-text recall."""
        if not source_id:
            raise ValueError("observations require a source_id (provenance is mandatory)")
        value_type = _validated_value_type(value, value_type)
        now = utc_isoformat()
        when = observed_at or now
        oid = new_id()
        await self.conn.execute(
            "INSERT INTO observations"
            " (id, subject_id, key, value, value_type, observed_at, source_id, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT (subject_id, key, observed_at) DO UPDATE SET"
            "  value = excluded.value, value_type = excluded.value_type,"
            "  source_id = excluded.source_id, created_at = excluded.created_at",
            (oid, subject_id, key, value, value_type, when, source_id, now),
        )
        await self.conn.commit()
        # Return the row as actually stored — on a restate (ON CONFLICT) the kept
        # row keeps its original id, so re-read rather than trust the new ULID.
        cur = await self.conn.execute(
            "SELECT * FROM observations WHERE subject_id = ? AND key = ? AND observed_at = ?",
            (subject_id, key, when),
        )
        return Observation.from_row(await cur.fetchone())

    async def observations_for(
        self,
        subject_id: str,
        *,
        key: str | None = None,
        since: str | None = None,
        limit: int = 100,
    ) -> list[Observation]:
        """A subject's datapoints, newest first — optionally one series (`key`) and/or
        since a UTC instant. Bounded so a long series can't blow up a read."""
        sql = "SELECT * FROM observations WHERE subject_id = ?"
        params: list = [subject_id]
        if key is not None:
            sql += " AND key = ?"
            params.append(key)
        if since is not None:
            sql += " AND observed_at >= ?"
            params.append(since)
        sql += " ORDER BY observed_at DESC LIMIT ?"
        params.append(limit)
        cur = await self.conn.execute(sql, tuple(params))
        return [Observation.from_row(r) for r in await cur.fetchall()]

    async def latest_observations(self, subject_id: str) -> list[Observation]:
        """The most recent datapoint in each of a subject's series — one row per
        key, for a compact 'what's the latest in every series' read."""
        cur = await self.conn.execute(
            "SELECT o.* FROM observations o"
            " JOIN (SELECT key, MAX(observed_at) AS mx FROM observations"
            "       WHERE subject_id = ? GROUP BY key) m"
            "   ON o.key = m.key AND o.observed_at = m.mx"
            " WHERE o.subject_id = ? ORDER BY o.key",
            (subject_id, subject_id),
        )
        return [Observation.from_row(r) for r in await cur.fetchall()]

    # ── relationships ─────────────────────────────────────────────────
    async def add_relationship(
        self,
        from_id: str,
        to_id: str,
        rel_type: str,
        *,
        source_id: str,
        directed: bool = True,
        confidence: float = 1.0,
        valid_from: str | None = None,
    ) -> Relationship:
        if not source_id:
            raise ValueError("relationships require a source_id (provenance is mandatory)")
        # Don't duplicate an identical current edge when a fact is restated.
        # For symmetric edges, a reversed match counts as the same.
        cur = await self.conn.execute(
            "SELECT * FROM relationships WHERE type = ? AND valid_to IS NULL"
            " AND from_id IN (?, ?) AND to_id IN (?, ?)",
            (rel_type, from_id, to_id, from_id, to_id),
        )
        for row in await cur.fetchall():
            existing = Relationship.from_row(row)
            same = existing.from_id == from_id and existing.to_id == to_id
            reversed_sym = not directed and existing.from_id == to_id and existing.to_id == from_id
            if same or reversed_sym:
                return existing
        now = utc_isoformat()
        rid = new_id()
        await self.conn.execute(
            "INSERT INTO relationships"
            " (id, from_id, to_id, type, valid_from, valid_to, directed, source_id,"
            "  confidence, created_at)"
            " VALUES (?, ?, ?, ?, ?, NULL, ?, ?, ?, ?)",
            (
                rid,
                from_id,
                to_id,
                rel_type,
                valid_from or now,
                1 if directed else 0,
                source_id,
                confidence,
                now,
            ),
        )
        await self.conn.commit()
        _publish_event({"type": "learned", "what": "link", "id": rid, "rel_type": rel_type,
                        "from_id": from_id, "to_id": to_id}, backfill=False)
        return Relationship(
            rid,
            from_id,
            to_id,
            rel_type,
            valid_from or now,
            None,
            directed,
            source_id,
            confidence,
            now,
        )

    async def relationships_for(
        self, subject_id: str, *, current_only: bool = True
    ) -> list[Relationship]:
        sql = "SELECT * FROM relationships WHERE (from_id = ? OR to_id = ?)"
        if current_only:
            sql += " AND valid_to IS NULL"
        cur = await self.conn.execute(sql, (subject_id, subject_id))
        return [Relationship.from_row(r) for r in await cur.fetchall()]

    async def relationships_with_source(
        self, subject_id: str
    ) -> list[tuple[Relationship, str]]:
        """Current relationships involving `subject_id`, each paired with its source
        KIND — so a lookup can hedge an inferred or web-read edge the same way it
        hedges a second-hand attribute. The provenance floor applies to edges too."""
        cur = await self.conn.execute(
            "SELECT r.*, s.kind AS source_kind FROM relationships r"
            " JOIN sources s ON s.id = r.source_id"
            " WHERE (r.from_id = ? OR r.to_id = ?) AND r.valid_to IS NULL",
            (subject_id, subject_id),
        )
        return [(Relationship.from_row(r), r["source_kind"]) for r in await cur.fetchall()]

    async def end_relationship(self, rel_id: str, *, valid_to: str | None = None) -> None:
        """Close a relationship (supersede-with-history): stamp `valid_to` so it
        stops being current but stays on the record. Nothing is ever deleted."""
        await self.conn.execute(
            "UPDATE relationships SET valid_to = ? WHERE id = ? AND valid_to IS NULL",
            (valid_to or utc_isoformat(), rel_id),
        )
        await self.conn.commit()

    # ── whole-graph scans (curiosity, dream) ──────────────────────────
    async def all_subjects(
        self, *, exclude_kinds: tuple[str, ...] = ("self",), limit: int = 1000, offset: int = 0,
        world_id: str | None = None,
    ) -> list[Subject]:
        """Every subject, for graph-wide scans. Excludes Moko's own `self` node
        by default; bounded by `limit` so a huge graph can't blow up a scan.
        `offset` pages through for the monitor's brain browser. `world_id` scopes the
        scan to one world (companion-facing curiosity must not chase Moko's-world nodes)."""
        holes = ",".join("?" for _ in exclude_kinds) or "''"
        world_clause = " AND world_id = ?" if world_id is not None else ""
        world_param = (world_id,) if world_id is not None else ()
        cur = await self.conn.execute(
            f"SELECT * FROM subjects WHERE kind NOT IN ({holes}) AND merged_into IS NULL"
            f"{world_clause} ORDER BY created_at LIMIT ? OFFSET ?",
            (*exclude_kinds, *world_param, limit, offset),
        )
        return [Subject.from_row(r) for r in await cur.fetchall()]

    async def _scan_valid[T](
        self, table: str, from_row: Callable[[aiosqlite.Row], T], *,
        current_only: bool = True, limit: int | None = None, offset: int = 0,
    ) -> list[T]:
        """Ordered scan of a versioned table — current rows only when `current_only`,
        paged for stable cursoring (the dream rotates a cursor over it). `table` is a
        trusted literal; SQLite can't parameterize a table name."""
        sql = f"SELECT * FROM {table}"
        if current_only:
            sql += " WHERE valid_to IS NULL"
        sql += " ORDER BY created_at"
        params: tuple = ()
        if limit is not None:
            sql += " LIMIT ? OFFSET ?"
            params = (limit, offset)
        cur = await self.conn.execute(sql, params)
        return [from_row(r) for r in await cur.fetchall()]

    async def all_relationships(
        self, *, current_only: bool = True, limit: int | None = None, offset: int = 0
    ) -> list[Relationship]:
        """Every relationship, for graph-wide scans. `ORDER BY created_at` makes
        paging stable across nights (the dream rotates a cursor over it)."""
        return await self._scan_valid(
            "relationships", Relationship.from_row,
            current_only=current_only, limit=limit, offset=offset,
        )

    async def all_attributes(
        self, *, current_only: bool = True, limit: int | None = None, offset: int = 0
    ) -> list[Attribute]:
        """Every attribute, in one query — so a graph-wide scan stays cheap
        instead of a per-subject round-trip. Ordered for stable paging."""
        return await self._scan_valid(
            "attributes", Attribute.from_row,
            current_only=current_only, limit=limit, offset=offset,
        )

    # ── messages (with perceived sentiment) ───────────────────────────
    async def add_message(
        self,
        channel: str,
        direction: str,
        body: str,
        *,
        speaker_id: str | None = None,
        sentiment: str | None = None,
        valence: float | None = None,
        arousal: float | None = None,
        sentiment_confidence: float | None = None,
        session_id: str | None = None,
        meta: str | None = None,
        sent_at: str | None = None,
    ) -> Message:
        now = utc_isoformat()
        mid = new_id()
        sent = sent_at or now
        await self.conn.execute(
            "INSERT INTO messages"
            " (id, channel, speaker_id, direction, body, sent_at, sentiment, valence, arousal,"
            "  sentiment_confidence, session_id, meta, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                mid,
                channel,
                speaker_id,
                direction,
                body,
                sent,
                sentiment,
                valence,
                arousal,
                sentiment_confidence,
                session_id,
                meta,
                now,
            ),
        )
        await self._index("message", mid, body)
        await self.conn.commit()
        # Observe: the store is the single writer, so this one hook catches
        # every message — inbound, replies, proactive outreach, salvage alike.
        _publish_event(
            {
                "type": "message",
                "id": mid,
                "channel": channel,
                "direction": direction,  # "in" (companion) | "out" (Moko)
                "body": body,
                "sentiment": sentiment,
                "session_id": session_id,
                "at": now,
            }
        )
        return Message(
            mid,
            channel,
            speaker_id,
            direction,
            body,
            sent,
            sentiment,
            valence,
            arousal,
            sentiment_confidence,
            session_id,
            meta,
            now,
        )

    # ── media (binary artifacts: images, voice notes, documents) ──────
    async def add_media(
        self,
        data: bytes,
        *,
        kind: str,
        mime: str | None = None,
        filename: str | None = None,
        caption: str | None = None,
        description: str | None = None,
        taken_at: str | None = None,
        message_id: str | None = None,
        channel: str | None = None,
        ref: str = "",
        direction: str = "in",
        source_kind: str = "message",
        world_id: str = COMPANION_WORLD,
    ) -> MediaArtifact:
        """Persist a media artifact: write the bytes to `~/.moko/media/` (content-
        addressed) and record a sourced row. Provenance is mandatory — the artifact
        gets a `source` like every fact (an inbound file is `kind=message`; what Moko
        later *infers* from it is a separate `vision`/`inference` source). `ref` is
        the channel handle to re-fetch it (e.g. a Telegram file_id). `description` is
        Moko's observation of what's in it (usually set later, on the vision turn).
        `world_id` is which world it belongs to (Moko's own selfies/world photos → Moko's;
        general renders + received files → the companion's, the default)."""
        # write_blob is synchronous and can move multi-MB of photo bytes to disk;
        # off-load it to a thread so the async loop isn't blocked (no blocking I/O
        # on the loop — CLAUDE.md).
        rel_name, sha, size = await asyncio.to_thread(
            media_blobs.write_blob, data, mime=mime, filename=filename
        )
        source = await self.add_source(source_kind, ref=ref, channel=channel, excerpt=caption)
        now = utc_isoformat()
        mid = new_id()
        await self.conn.execute(
            "INSERT INTO media"
            " (id, source_id, message_id, kind, mime, sha256, bytes, path, filename, caption,"
            "  description, direction, created_at, taken_at, world_id)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (mid, source.id, message_id, kind, mime, sha, size, rel_name, filename, caption,
             description, direction, now, taken_at, world_id),
        )
        if description and description.strip():
            await self._index("media", mid, description.strip())
        await self.conn.commit()
        return MediaArtifact(
            mid, source.id, message_id, kind, mime, sha, size, rel_name, filename, caption,
            description, direction, now, taken_at=taken_at, world_id=world_id,
        )

    async def set_media_description(self, media_id: str, description: str) -> None:
        """Record Moko's observation of what an image showed — the durable gist that
        survives session rotation, and is searchable via `recall` ("that dog photo").
        Re-indexes so an updated observation replaces the old one."""
        desc = (description or "").strip()
        if not desc:
            return
        await self.conn.execute(
            "UPDATE media SET description = ? WHERE id = ?", (desc, media_id)
        )
        await self._unindex(media_id)  # drop any prior entry before re-indexing
        await self._index("media", media_id, desc)
        await self.conn.commit()

    async def media_by_sha(
        self, sha256: str, *, direction: str | None = None
    ) -> MediaArtifact | None:
        """The most recent artifact with this exact content (optionally in one direction) —
        for resolving the id of a file being sent (it may already be on record from when Moko
        generated it), so the feed can deep-link a thumbnail."""
        if direction:
            cur = await self.conn.execute(
                "SELECT * FROM media WHERE sha256 = ? AND direction = ?"
                " ORDER BY created_at DESC LIMIT 1", (sha256, direction))
        else:
            cur = await self.conn.execute(
                "SELECT * FROM media WHERE sha256 = ? ORDER BY created_at DESC LIMIT 1", (sha256,))
        row = await cur.fetchone()
        return MediaArtifact.from_row(row) if row is not None else None

    async def media_for_message(self, message_id: str) -> list[MediaArtifact]:
        """Artifacts that rode in on one message (oldest-first)."""
        cur = await self.conn.execute(
            "SELECT * FROM media WHERE message_id = ? ORDER BY created_at", (message_id,)
        )
        return [MediaArtifact.from_row(r) for r in await cur.fetchall()]

    async def recent_media(
        self, *, limit: int = 10, offset: int = 0, world_id: str | None = None
    ) -> list[MediaArtifact]:
        """The most recently stored artifacts, newest-first — for the monitor and
        for `lookup`-style recall of "that photo you sent". `world_id` scopes to one world
        (so the gallery's world filter selects in SQL, not over a fetched window)."""
        world_clause = " WHERE world_id = ?" if world_id is not None else ""
        world_param = (world_id,) if world_id is not None else ()
        cur = await self.conn.execute(
            f"SELECT * FROM media{world_clause} ORDER BY created_at DESC LIMIT ? OFFSET ?",
            (*world_param, limit, offset),
        )
        return [MediaArtifact.from_row(r) for r in await cur.fetchall()]

    async def media_by_id(self, media_id: str) -> MediaArtifact | None:
        """One artifact by id — for the monitor's media viewer to serve its bytes."""
        cur = await self.conn.execute("SELECT * FROM media WHERE id = ?", (media_id,))
        row = await cur.fetchone()
        return MediaArtifact.from_row(row) if row is not None else None

    async def media_by_path(self, rel_path: str) -> MediaArtifact | None:
        """One artifact by its stored relative path — maps a canon attribute (which records
        the blob's path) back to the media row, so the monitor can serve it by id. Content-
        addressed paths can repeat across rows (identical bytes re-saved); newest wins."""
        cur = await self.conn.execute(
            "SELECT * FROM media WHERE path = ? ORDER BY created_at DESC LIMIT 1", (rel_path,)
        )
        row = await cur.fetchone()
        return MediaArtifact.from_row(row) if row is not None else None

    async def generated_media_counts(self) -> dict[str, int]:
        """How many artifacts Moko has *generated* (outbound), by kind — image/audio/video.
        Media generation isn't token-priced, so the Usage tab shows it as units alongside the
        token ledger."""
        cur = await self.conn.execute(
            "SELECT kind, COUNT(*) AS n FROM media WHERE direction = 'out' GROUP BY kind"
        )
        return {r["kind"]: r["n"] for r in await cur.fetchall()}

    async def messages_for_session(self, session_id: str, *, limit: int = 20) -> list[Message]:
        """The most recent messages in a session, oldest-first."""
        cur = await self.conn.execute(
            "SELECT * FROM messages WHERE session_id = ? ORDER BY sent_at DESC LIMIT ?",
            (session_id, limit),
        )
        rows = list(await cur.fetchall())
        return [Message.from_row(r) for r in reversed(rows)]

    async def last_message_at(self, *, session_id: str | None = None) -> str | None:
        """The most recent message time (UTC iso) — across all sessions, or within one
        `session_id` — used to tell whether the companion is actively chatting."""
        sql = "SELECT MAX(sent_at) AS t FROM messages"
        params: tuple = ()
        if session_id is not None:
            sql += " WHERE session_id = ?"
            params = (session_id,)
        cur = await self.conn.execute(sql, params)
        row = await cur.fetchone()
        return row["t"] if row is not None else None

    async def latest_messages(self, *, limit: int = 8) -> list[Message]:
        """The most recent messages across all sessions, oldest-first — the short
        transcript the brain reads when deciding whether/when to reach out."""
        cur = await self.conn.execute(
            "SELECT * FROM messages ORDER BY sent_at DESC LIMIT ?", (limit,)
        )
        rows = list(await cur.fetchall())
        return [Message.from_row(r) for r in reversed(rows)]

    async def list_messages(self, *, limit: int = 50, offset: int = 0) -> list[Message]:
        """Messages newest-first, paged — the monitor's brain browser. Distinct
        from `latest_messages` (which the brain reads oldest-first as a transcript);
        kept separate so paging concerns never touch that hot read path."""
        cur = await self.conn.execute(
            "SELECT * FROM messages ORDER BY sent_at DESC LIMIT ? OFFSET ?", (limit, offset)
        )
        return [Message.from_row(r) for r in await cur.fetchall()]

    async def set_message_sentiment(
        self,
        message_id: str,
        *,
        sentiment: str | None,
        valence: float | None = None,
        arousal: float | None = None,
        confidence: float | None = None,
    ) -> None:
        await self.conn.execute(
            "UPDATE messages SET sentiment = ?, valence = ?, arousal = ?,"
            " sentiment_confidence = ? WHERE id = ?",
            (sentiment, valence, arousal, confidence, message_id),
        )
        await self.conn.commit()

    # ── commitments (things Moko owes the companion) ──────────────────
    async def add_commitment(
        self,
        description: str,
        *,
        kind: str = "reminder",
        about_id: str | None = None,
        due_at: str | None = None,
        source_id: str | None = None,
    ) -> Commitment:
        now = utc_isoformat()
        cid = new_id()
        await self.conn.execute(
            "INSERT INTO commitments"
            " (id, description, kind, about_id, due_at, status, source_id, created_at)"
            " VALUES (?, ?, ?, ?, ?, 'open', ?, ?)",
            (cid, description, kind, about_id, due_at, source_id, now),
        )
        await self.conn.commit()
        _publish_event({"type": "commitment.created", "id": cid, "kind": kind,
                        "description": description, "due_at": due_at})
        return Commitment(cid, description, kind, about_id, due_at, "open", now)

    async def due_commitments(self, as_of: str) -> list[Commitment]:
        """Open commitments whose due_at has passed (the heartbeat's worklist)."""
        cur = await self.conn.execute(
            "SELECT * FROM commitments WHERE status = 'open'"
            " AND due_at IS NOT NULL AND due_at <= ? ORDER BY due_at",
            (as_of,),
        )
        return [Commitment.from_row(r) for r in await cur.fetchall()]

    async def open_commitments(self, limit: int = 50, offset: int = 0) -> list[Commitment]:
        cur = await self.conn.execute(
            "SELECT * FROM commitments WHERE status = 'open'"
            " ORDER BY created_at LIMIT ? OFFSET ?",
            (limit, offset),
        )
        return [Commitment.from_row(r) for r in await cur.fetchall()]

    async def recent_commitments(self, *, within_hours: int = 48) -> list[Commitment]:
        """Commitments created within the last `within_hours`, *any* status — so
        re-promising the same thing soon after isn't recorded twice (the heartbeat
        already surfaced and closed the first). Intent-dedup uses this so a staggered
        burst of the same promise can't pile up."""
        cutoff = (now_utc() - dt.timedelta(hours=within_hours)).isoformat()
        cur = await self.conn.execute(
            "SELECT * FROM commitments WHERE created_at >= ? ORDER BY created_at DESC",
            (cutoff,),
        )
        return [Commitment.from_row(r) for r in await cur.fetchall()]

    async def list_commitments(self, *, limit: int = 50, offset: int = 0) -> list[Commitment]:
        """Every commitment regardless of status, newest-first, paged — the
        monitor's brain browser (so done/cancelled ones stay visible, not just
        the open ones the brain acts on)."""
        cur = await self.conn.execute(
            "SELECT * FROM commitments ORDER BY created_at DESC LIMIT ? OFFSET ?",
            (limit, offset),
        )
        return [Commitment.from_row(r) for r in await cur.fetchall()]

    async def complete_commitment(self, commitment_id: str) -> None:
        cur = await self.conn.execute("SELECT description FROM commitments WHERE id = ?",
                                      (commitment_id,))
        row = await cur.fetchone()
        await self.conn.execute(
            "UPDATE commitments SET status = 'done', completed_at = ? WHERE id = ?",
            (utc_isoformat(), commitment_id),
        )
        await self.conn.commit()
        _publish_event({"type": "commitment.completed", "id": commitment_id,
                        "description": row["description"] if row else None})


    # ── sessions (working memory) ─────────────────────────────────────
    async def current_session(self) -> Session | None:
        """The active session (not yet rotated), if any."""
        cur = await self.conn.execute(
            "SELECT * FROM sessions WHERE ended_at IS NULL ORDER BY started_at DESC LIMIT 1"
        )
        row = await cur.fetchone()
        return Session.from_row(row) if row is not None else None

    async def start_session(self, reason: str = "user", *, carryover: str | None = None) -> Session:
        """Begin a fresh session, ending any currently-active one.

        Rotation *policy* (when to call this — after dream, on request, on
        size, on idle) lives with the agent loop; this just does the swap.
        Callers must ensure the prior session is consolidated first.
        """
        now = utc_isoformat()
        active = await self.current_session()
        if active is not None:
            await self.conn.execute(
                "UPDATE sessions SET ended_at = ?, ended_reason = ? WHERE id = ?",
                (now, reason, active.id),
            )
        sid = new_id()
        await self.conn.execute(
            "INSERT INTO sessions (id, started_at, started_reason, carryover, token_estimate)"
            " VALUES (?, ?, ?, ?, 0)",
            (sid, now, reason, carryover),
        )
        await self.conn.commit()
        return Session(sid, now, None, reason, None, carryover, 0)

    async def ensure_session(self) -> Session:
        """Return the active session, starting the first one if needed."""
        active = await self.current_session()
        if active is not None:
            return active
        return await self.start_session("first")

    async def end_session(self, reason: str) -> None:
        active = await self.current_session()
        if active is not None:
            await self.conn.execute(
                "UPDATE sessions SET ended_at = ?, ended_reason = ? WHERE id = ?",
                (utc_isoformat(), reason, active.id),
            )
            await self.conn.commit()

    async def get_session(self, session_id: str) -> Session | None:
        cur = await self.conn.execute("SELECT * FROM sessions WHERE id = ?", (session_id,))
        row = await cur.fetchone()
        return Session.from_row(row) if row is not None else None

    async def add_session_tokens(self, session_id: str, tokens: int) -> None:
        """Bump a session's rough size estimate (drives size-based rotation)."""
        await self.conn.execute(
            "UPDATE sessions SET token_estimate = token_estimate + ? WHERE id = ?",
            (tokens, session_id),
        )
        await self.conn.commit()

    async def set_session_tokens(self, session_id: str, tokens: int) -> None:
        """Set a session's size estimate (e.g. to the latest context size)."""
        await self.conn.execute(
            "UPDATE sessions SET token_estimate = ? WHERE id = ?",
            (tokens, session_id),
        )
        await self.conn.commit()

    # ── thoughts (Moko's private inner life) ──────────────────────────
    async def add_thought(
        self,
        kind: str,
        body: str,
        *,
        valence: float | None = None,
        shareability: str = "private",
        prudence: float = 1.0,
        stability: float = 0.5,
        links: list[tuple[str, str]] | None = None,
        world_id: str = COMPANION_WORLD,
    ) -> Thought:
        now = utc_isoformat()
        tid = new_id()
        await self.conn.execute(
            "INSERT INTO thoughts"
            " (id, kind, body, valence, shareability, prudence, stability, created_at, world_id)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (tid, kind, body, valence, shareability, prudence, stability, now, world_id),
        )
        for target_kind, target_id in links or []:
            await self.conn.execute(
                "INSERT INTO thought_links (id, thought_id, target_kind, target_id)"
                " VALUES (?, ?, ?, ?)",
                (new_id(), tid, target_kind, target_id),
            )
        await self._index("thought", tid, body)
        await self.conn.commit()
        # Observe: Moko's inner life as it forms. Private by default — observers
        # (and the monitor UI) must treat shareability with care.
        _publish_event(
            {
                "type": "thought",
                "id": tid,
                "kind": kind,
                "body": body,
                "valence": valence,
                "shareability": shareability,
                "at": now,
            }
        )
        return Thought(tid, kind, body, valence, shareability, prudence, stability, now,
                       world_id=world_id)

    async def counts(self) -> dict[str, int]:
        """A snapshot of how big Moko's mind is, for the monitor's status panel.
        One cheap COUNT(*) per table; labels are UI-facing, not table names.

        `subjects` carries the same filter the brain browser lists by — Moko's own
        `self` node and merged-away duplicates excluded (see `all_subjects`) — so the
        tab badge can never disagree with the rows shown beneath it."""
        # (table, extra WHERE) per UI label; both are trusted literals.
        specs: dict[str, tuple[str, str]] = {
            "subjects": ("subjects", "WHERE kind != 'self' AND merged_into IS NULL"),
            "attributes": ("attributes", ""),
            "relationships": ("relationships", ""),
            "messages": ("messages", ""),
            "thoughts": ("thoughts", ""),
            "gaps": ("knowledge_gaps", ""),
            "commitments": ("commitments", ""),
            "dreams": ("dreams", ""),
            "sessions": ("sessions", ""),
        }
        out: dict[str, int] = {}
        for label, (table, where) in specs.items():
            cur = await self.conn.execute(f"SELECT COUNT(*) AS n FROM {table} {where}")  # noqa: S608 — fixed names
            row = await cur.fetchone()
            out[label] = int(row["n"]) if row else 0
        return out

    async def link_thought(self, thought_id: str, target_kind: str, target_id: str) -> None:
        """Link an existing thought to what it's about (subject/fact/message/gap).
        Idempotent-ish: callers pass fresh links, so duplicates aren't created in
        the normal write path."""
        await self.conn.execute(
            "INSERT INTO thought_links (id, thought_id, target_kind, target_id)"
            " VALUES (?, ?, ?, ?)",
            (new_id(), thought_id, target_kind, target_id),
        )
        await self.conn.commit()

    async def thoughts_linked_to(self, subject_id: str, *, limit: int = 6) -> list[Thought]:
        """Moko's own past thoughts about one subject — the precise inner-life
        read behind `lookup`. Ranked by prudence (weight Moko still gives them),
        then recency. Faded thoughts still surface; they just rank lower."""
        cur = await self.conn.execute(
            "SELECT t.* FROM thoughts t"
            " JOIN thought_links l ON l.thought_id = t.id"
            " WHERE l.target_kind = 'subject' AND l.target_id = ?"
            " ORDER BY t.prudence DESC, t.created_at DESC LIMIT ?",
            (subject_id, limit),
        )
        return [Thought.from_row(r) for r in await cur.fetchall()]

    async def linked_subjects_for_thought(self, thought_id: str) -> list[Subject]:
        """The subjects one thought is *about* — its `thought_links` of kind
        'subject'. The inverse of `thoughts_linked_to`: lets the monitor walk from a
        thought to its subjects (and through them to the gaps about those subjects),
        so a thought and a same-subject curiosity are navigable to each other."""
        cur = await self.conn.execute(
            "SELECT e.* FROM subjects e"
            " JOIN thought_links l ON l.target_id = e.id"
            " WHERE l.thought_id = ? AND l.target_kind = 'subject' AND e.merged_into IS NULL",
            (thought_id,),
        )
        return [Subject.from_row(r) for r in await cur.fetchall()]

    async def thoughts_about(self, query: str, *, limit: int = 6) -> list[Thought]:
        """Moko's past thoughts matching a topic by text (FTS over thought
        bodies) — the broad `reflect(about=…)` read. Catches thoughts whether or
        not they were linked to a subject. Ranked by prudence, then recency."""
        match = self._fts_match(query)
        if match is None:
            return []
        cur = await self.conn.execute(
            "SELECT t.* FROM search s"
            " JOIN thoughts t ON t.id = s.ref_id"
            " WHERE s.kind = 'thought' AND s.content MATCH ?"
            " ORDER BY t.prudence DESC, t.created_at DESC LIMIT ?",
            (match, limit),
        )
        return [Thought.from_row(r) for r in await cur.fetchall()]

    async def list_thoughts(
        self, *, limit: int = 100, offset: int = 0, world_id: str | None = None
    ) -> list[Thought]:
        """Every thought, most-recent first — the unfiltered inner life, for the
        monitor's brain browser (unlike `recent_thoughts`, keeps notes + faded).
        `offset` pages through the full history; `world_id` scopes to one world."""
        where = "WHERE world_id = ?" if world_id is not None else ""
        params = ([world_id] if world_id is not None else []) + [limit, offset]
        cur = await self.conn.execute(
            f"SELECT * FROM thoughts {where} ORDER BY created_at DESC LIMIT ? OFFSET ?", params
        )
        return [Thought.from_row(r) for r in await cur.fetchall()]

    async def get_thought(self, thought_id: str) -> Thought | None:
        cur = await self.conn.execute("SELECT * FROM thoughts WHERE id = ?", (thought_id,))
        row = await cur.fetchone()
        return Thought.from_row(row) if row else None

    async def recent_thoughts(
        self, *, limit: int = 3, min_prudence: float = 0.3
    ) -> list[Thought]:
        """The handful of Moko's most recent, still-weighty thoughts — the
        ambient inner life injected each turn so mood/opinion carries across
        sessions. Excludes plain `note`s (housekeeping, not feeling) and thoughts
        that have faded below `min_prudence`."""
        cur = await self.conn.execute(
            "SELECT * FROM thoughts WHERE kind != 'note' AND prudence >= ?"
            " ORDER BY created_at DESC LIMIT ?",
            (min_prudence, limit),
        )
        return [Thought.from_row(r) for r in await cur.fetchall()]

    # ── knowledge gaps (curiosity) ────────────────────────────────────
    async def add_gap(
        self,
        description: str,
        kind: str,
        *,
        about_id: str | None = None,
        resolution_path: str = "either",
        salience: float = 0.5,
        status: str = "open",
        world_id: str = COMPANION_WORLD,
    ) -> Gap:
        now = utc_isoformat()
        gid = new_id()
        await self.conn.execute(
            "INSERT INTO knowledge_gaps"
            " (id, about_id, description, kind, resolution_path, salience, status, opened_at,"
            "  world_id)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (gid, about_id, description, kind, resolution_path, salience, status, now, world_id),
        )
        await self.conn.commit()
        # Observe: a new curiosity forming. One hook here catches them all —
        # from a turn, a reflection, a dream, or gap detection.
        _publish_event(
            {
                "type": "gap.opened",
                "id": gid,
                "description": description,
                "kind": kind,
                "resolution_path": resolution_path,
                "salience": salience,
            }
        )
        return Gap(gid, about_id, description, kind, resolution_path, salience, status, now,
                   world_id=world_id)

    async def open_gaps(
        self, limit: int = 20, offset: int = 0, *, world_id: str | None = None
    ) -> list[Gap]:
        where = " AND world_id = ?" if world_id is not None else ""
        params = ([world_id] if world_id is not None else []) + [limit, offset]
        cur = await self.conn.execute(
            f"SELECT * FROM knowledge_gaps WHERE status = 'open'{where}"
            " ORDER BY salience DESC LIMIT ? OFFSET ?",
            params,
        )
        return [Gap.from_row(r) for r in await cur.fetchall()]

    async def list_gaps(
        self, *, limit: int = 20, offset: int = 0, world_id: str | None = None
    ) -> list[Gap]:
        """Every gap regardless of status — active ones first (open/researching/asked),
        then resolved/abandoned — the monitor's brain browser (so completed/archived
        curiosities stay visible, not just the open ones the brain acts on, and the tab
        badge matches the rows). Within each group, most-salient then newest first.
        Distinct from `open_gaps`, which the brain uses for the *actionable* worklist."""
        where = " WHERE world_id = ?" if world_id is not None else ""
        params = ([world_id] if world_id is not None else []) + [limit, offset]
        cur = await self.conn.execute(
            f"SELECT * FROM knowledge_gaps{where}"
            " ORDER BY CASE WHEN status IN ('resolved', 'abandoned') THEN 1 ELSE 0 END,"
            " salience DESC, opened_at DESC LIMIT ? OFFSET ?",
            params,
        )
        return [Gap.from_row(r) for r in await cur.fetchall()]

    async def gap_signatures(self) -> set[tuple[str, str | None, str]]:
        """The (kind, about_id, description) of EVERY gap, any status — the dedup set for gap
        detection, so a resolved or abandoned gap is NOT re-created identically. (The bug this
        fixes: deduping against open gaps only meant a resolved dangling-ref gap was re-detected
        and re-written each turn the subject stayed thin, piling up N resolved duplicates.)"""
        cur = await self.conn.execute(
            "SELECT DISTINCT kind, about_id, description FROM knowledge_gaps"
        )
        return {(r["kind"], r["about_id"], r["description"]) for r in await cur.fetchall()}

    async def get_gap(self, gap_id: str) -> Gap | None:
        cur = await self.conn.execute("SELECT * FROM knowledge_gaps WHERE id = ?", (gap_id,))
        row = await cur.fetchone()
        return Gap.from_row(row) if row else None

    async def gaps_about(self, subject_id: str, *, limit: int = 20) -> list[Gap]:
        """Every gap whose subject is this subject (any status) — open curiosities
        first, then most salient. The subject-mediated link between a curiosity and
        a subject, surfaced for the monitor so a thought about someone and a gap
        about that same someone show up on each other's detail view."""
        cur = await self.conn.execute(
            "SELECT * FROM knowledge_gaps WHERE about_id = ?"
            " ORDER BY (status = 'open') DESC, salience DESC LIMIT ?",
            (subject_id, limit),
        )
        return [Gap.from_row(r) for r in await cur.fetchall()]

    async def next_curiosity_gap(self) -> Gap | None:
        """The best open gap to raise in conversation: only `ask`/`either` paths
        (research-path gaps Moko answers itself), never-surfaced first, then by
        salience, then least-recently raised — so questions rotate and pace."""
        cur = await self.conn.execute(
            "SELECT * FROM knowledge_gaps WHERE status='open'"
            " AND resolution_path IN ('ask', 'either')"
            " ORDER BY (last_asked_at IS NOT NULL), salience DESC, last_asked_at ASC LIMIT 1"
        )
        row = await cur.fetchone()
        return Gap.from_row(row) if row else None

    async def next_research_gap(self) -> Gap | None:
        """The top open `research`-path gap — a world-fact Moko can look up
        itself (vs. `ask` gaps, which only the companion can answer)."""
        cur = await self.conn.execute(
            "SELECT * FROM knowledge_gaps WHERE status='open' AND resolution_path='research'"
            " ORDER BY salience DESC LIMIT 1"
        )
        row = await cur.fetchone()
        return Gap.from_row(row) if row else None

    async def count_research_gaps(self) -> int:
        """How many open research-path gaps are queued — the rhythm view's
        'research' depth (it runs one per idle tick, so this is the backlog)."""
        cur = await self.conn.execute(
            "SELECT COUNT(*) AS c FROM knowledge_gaps"
            " WHERE status='open' AND resolution_path='research'"
        )
        return (await cur.fetchone())["c"]

    async def mark_gap_surfaced(self, gap_id: str) -> None:
        """Record that a gap was raised in conversation (stamps `last_asked_at`
        to pace re-asking) without changing its status — it stays `open` until
        actually resolved."""
        await self.conn.execute(
            "UPDATE knowledge_gaps SET last_asked_at = ? WHERE id = ?",
            (utc_isoformat(), gap_id),
        )
        await self.conn.commit()

    async def resolve_satisfied_gaps(self) -> int:
        """Close gaps whose structural condition is now met, one set-based pass
        each (no per-gap round-trip):

          • a `dangling_ref` about a subject that has since gained facts;
          • a `missing_attr` about a subject that has since gained an attribute
            whose key appears in the gap's question — e.g. "Biscuit's age" once
            an `age` attribute lands — so Moko stops asking what it now knows.

        Conservative for `missing_attr`: the attribute must be *new* (created at
        or after the gap opened) and its key a real word (length ≥ 3), so a
        coincidental short-key match can't wrongly close an open question. The
        fuzzier cases (no `about_id`, free-text answers) are left to the dream's
        LLM pass. Returns how many closed."""
        closed = 0
        closed += await self._close_satisfied(
            "kind='dangling_ref' AND about_id IS NOT NULL"
            " AND EXISTS (SELECT 1 FROM attributes a"
            " WHERE a.subject_id = knowledge_gaps.about_id AND a.valid_to IS NULL)"
        )
        closed += await self._close_satisfied(
            "kind='missing_attr' AND about_id IS NOT NULL"
            " AND EXISTS (SELECT 1 FROM attributes a"
            " WHERE a.subject_id = knowledge_gaps.about_id AND a.valid_to IS NULL"
            " AND a.created_at >= knowledge_gaps.opened_at AND length(a.key) >= 3"
            " AND instr(lower(knowledge_gaps.description), lower(a.key)) > 0)"
        )
        return closed

    async def _close_satisfied(self, condition: str) -> int:
        """Close every open gap matching `condition`, emitting a `gap.resolved`
        event per gap so structural resolution is as visible in the monitor's feed
        as the inline kind (`set_gap_status`). Select-then-close (not a blind bulk
        UPDATE) is what lets us name each gap as it closes. Returns the count."""
        cur = await self.conn.execute(
            f"SELECT id, description FROM knowledge_gaps WHERE status='open' AND {condition}"
        )
        rows = await cur.fetchall()
        if not rows:
            return 0
        ids = [r["id"] for r in rows]
        await self.conn.execute(
            f"UPDATE knowledge_gaps SET status='resolved' WHERE id IN ({','.join('?' * len(ids))})",
            ids,
        )
        await self.conn.commit()
        for r in rows:
            _publish_event(
                {"type": "gap.resolved", "id": r["id"], "description": r["description"]}
            )
        return len(ids)

    async def set_gap_status(
        self, gap_id: str, status: str, *, resolved_by: str | None = None
    ) -> None:
        """Move a gap along its lifecycle (open → researching/queued/asked →
        resolved/abandoned), stamping the matching timestamp so the proactivity
        governor (M5) can pace retries and backoff."""
        sets, params = ["status = ?"], [status]
        stamp = {"researching": "last_attempt_at", "asked": "last_asked_at"}.get(status)
        if stamp:
            sets.append(f"{stamp} = ?")
            params.append(utc_isoformat())
        if resolved_by is not None:
            sets.append("resolved_by = ?")
            params.append(resolved_by)
        params.append(gap_id)
        await self.conn.execute(
            f"UPDATE knowledge_gaps SET {', '.join(sets)} WHERE id = ?", params
        )
        await self.conn.commit()
        if status == "resolved":
            # Symmetric to gap.opened: surface a curiosity *closing* so the monitor's
            # feed shows resolution happening — inline (answered in chat) or along the
            # lifecycle — instead of gaps silently vanishing from the open list.
            cur = await self.conn.execute(
                "SELECT description FROM knowledge_gaps WHERE id = ?", (gap_id,)
            )
            row = await cur.fetchone()
            _publish_event(
                {
                    "type": "gap.resolved",
                    "id": gap_id,
                    "description": row["description"] if row else "",
                }
            )

    # ── outreach log (proactivity governor's memory) ──────────────────
    async def log_outreach(
        self,
        channel: str,
        kind: str,
        *,
        gap_id: str | None = None,
        counts_to_budget: bool = True,
        engaged: bool | None = None,
    ) -> str:
        oid = new_id()
        await self.conn.execute(
            "INSERT INTO outreach"
            " (id, channel, kind, gap_id, counts_to_budget, sent_at, engaged)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                oid,
                channel,
                kind,
                gap_id,
                1 if counts_to_budget else 0,
                utc_isoformat(),
                None if engaged is None else int(engaged),
            ),
        )
        await self.conn.commit()
        return oid

    async def count_budget_outreach(self, since: str) -> int:
        """Budget-counted outreaches sent since `since` (ISO UTC) — the rolling
        window the proactivity governor checks curiosity against."""
        cur = await self.conn.execute(
            "SELECT COUNT(*) AS c FROM outreach"
            " WHERE counts_to_budget = 1 AND sent_at >= ?",
            (since,),
        )
        return (await cur.fetchone())["c"]

    async def oldest_budget_outreach_since(self, since: str) -> str | None:
        """The earliest budget-counted outreach in the rolling window — so the
        rhythm view can say when the daily ceiling next frees up (oldest + 24h)."""
        cur = await self.conn.execute(
            "SELECT MIN(sent_at) AS t FROM outreach"
            " WHERE counts_to_budget = 1 AND sent_at >= ?",
            (since,),
        )
        row = await cur.fetchone()
        return row["t"] if row and row["t"] else None

    async def last_outreach_at(self) -> str | None:
        """When Moko last reached out (any kind), for pacing unprompted messages."""
        cur = await self.conn.execute("SELECT MAX(sent_at) AS t FROM outreach")
        row = await cur.fetchone()
        return row["t"] if row else None

    async def mark_outreach_engaged(self) -> None:
        """Mark the most recent un-acked outreach as engaged — called when the
        companion sends anything, so backoff resets once they re-engage."""
        await self.conn.execute(
            "UPDATE outreach SET engaged = 1 WHERE id = ("
            " SELECT id FROM outreach WHERE engaged IS NULL ORDER BY sent_at DESC LIMIT 1)"
        )
        await self.conn.commit()

    async def outreach_kind_count(self, kind: str, since: str) -> int:
        """Outreaches of one kind sent since `since` (ISO UTC) — used to pace a
        kind on its own (e.g. at most one well-being check-in a day)."""
        cur = await self.conn.execute(
            "SELECT COUNT(*) AS c FROM outreach WHERE kind = ? AND sent_at >= ?",
            (kind, since),
        )
        return (await cur.fetchone())["c"]

    # ── app state (small kv scratch shared between ticks) ─────────────
    async def get_state(self, key: str) -> str | None:
        cur = await self.conn.execute("SELECT value FROM app_state WHERE key = ?", (key,))
        row = await cur.fetchone()
        return row["value"] if row is not None else None

    async def set_state(self, key: str, value: str) -> None:
        await self.conn.execute(
            "INSERT INTO app_state (key, value) VALUES (?, ?)"
            " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        await self.conn.commit()

    async def clear_state(self, key: str) -> None:
        await self.conn.execute("DELETE FROM app_state WHERE key = ?", (key,))
        await self.conn.commit()

    async def recent_affect(self, *, limit: int = 8) -> list[dict]:
        """The companion's recent message affect — valence (-1..1), arousal (0..1),
        the read's confidence (0..1), and the free-text sentiment label — newest
        first. The well-being trend uses the three numeric axes (gate on arousal:
        keyed-up isn't low mood; weight by confidence: an unsure read shouldn't
        drive an outreach); the label rides along for future dispositional/label
        aggregation (see SCHEMA.md § messages). Skips messages with no sentiment
        recorded — a terse, un-annotated message contributes nothing, the trend's
        known coverage limit."""
        cur = await self.conn.execute(
            "SELECT valence, arousal, sentiment_confidence AS confidence, sentiment"
            " FROM messages WHERE direction = 'in' AND valence IS NOT NULL"
            " ORDER BY sent_at DESC LIMIT ?",
            (limit,),
        )
        return [
            {
                "valence": r["valence"],
                "arousal": r["arousal"],
                "confidence": r["confidence"],
                "sentiment": r["sentiment"],
            }
            for r in await cur.fetchall()
        ]

    # ── dream (nightly consolidation) ─────────────────────────────────
    async def start_dream(self, *, at: str | None = None) -> str:
        did = new_id()
        await self.conn.execute(
            "INSERT INTO dreams (id, started_at) VALUES (?, ?)", (did, at or utc_isoformat())
        )
        await self.conn.commit()
        return did

    async def finish_dream(
        self, dream_id: str, *, summary: str = "", stats: dict | None = None, wonders: str = ""
    ) -> None:
        await self.conn.execute(
            "UPDATE dreams SET ended_at = ?, summary = ?, stats = ?, wonders = ? WHERE id = ?",
            (utc_isoformat(), summary, json.dumps(stats or {}), wonders, dream_id),
        )
        await self.conn.commit()

    async def recent_dreams(self, *, limit: int = 5, offset: int = 0) -> list[dict]:
        """The dream journal, newest first — when Moko consolidated and what it did."""
        cur = await self.conn.execute(
            "SELECT id, started_at, ended_at, summary, stats, wonders FROM dreams"
            " ORDER BY started_at DESC LIMIT ? OFFSET ?",
            (limit, offset),
        )
        return [dict(r) for r in await cur.fetchall()]

    async def recent_outreach(self, *, limit: int = 10) -> list[dict]:
        """Recent unprompted messages Moko sent, newest first."""
        cur = await self.conn.execute(
            "SELECT sent_at, channel, kind, engaged FROM outreach"
            " ORDER BY sent_at DESC LIMIT ?",
            (limit,),
        )
        return [dict(r) for r in await cur.fetchall()]

    # ── action log (what Moko did — for cross-turn 'recently you…' awareness) ──
    async def log_action(self, kind: str, summary: str, *, ok: bool = True) -> None:
        """Record an action Moko took, so it stays aware of what it did across
        turns/sessions (closing the 'what happened?' loop)."""
        await self.conn.execute(
            "INSERT INTO actions (id, at, kind, summary, ok) VALUES (?, ?, ?, ?, ?)",
            (new_id(), utc_isoformat(), kind, summary, 1 if ok else 0),
        )
        await self.conn.commit()

    async def recent_actions(self, *, limit: int = 5) -> list[dict]:
        """The last few actions Moko took, newest first."""
        cur = await self.conn.execute(
            "SELECT at, kind, summary, ok FROM actions ORDER BY at DESC LIMIT ?",
            (limit,),
        )
        return [dict(r) for r in await cur.fetchall()]

    async def record_outreach_decision(
        self, *, acted: bool, candidate: str | None, reasoning: str | None,
        reconsider_minutes: float | None,
    ) -> str:
        """Persist one reach-out deliberation — including a decision to wait — so
        the choice is durable, observable history (not just a transient event)."""
        cid = new_id()
        await self.conn.execute(
            "INSERT INTO outreach_decisions"
            " (id, at, acted, candidate, reasoning, reconsider_minutes) VALUES (?, ?, ?, ?, ?, ?)",
            (cid, utc_isoformat(), int(bool(acted)), candidate, reasoning,
             float(reconsider_minutes) if reconsider_minutes is not None else None),
        )
        await self.conn.commit()
        return cid

    async def recent_outreach_decisions(self, *, limit: int = 20) -> list[dict]:
        """Recent reach-out deliberations, newest first."""
        cur = await self.conn.execute(
            "SELECT at, acted, candidate, reasoning, reconsider_minutes FROM outreach_decisions"
            " ORDER BY at DESC LIMIT ?",
            (limit,),
        )
        return [dict(r) for r in await cur.fetchall()]

    async def last_dream_started_at(self, since: str) -> str | None:
        """The start time (ISO UTC) of the most recent dream begun since `since`, or None
        if none tonight — so the scheduler can space repeated dreams within the sleep
        window by an interval (Moko dreams several times a night, not once)."""
        cur = await self.conn.execute(
            "SELECT started_at FROM dreams WHERE started_at >= ? ORDER BY started_at DESC"
            " LIMIT 1",
            (since,),
        )
        row = await cur.fetchone()
        return row["started_at"] if row else None

    async def close_orphaned_dreams(self, *, before: str) -> int:
        """Mark any dream started before `before` that never finished as ended — a
        hard-kill leftover — so the journal doesn't show a perpetual 'in progress'.
        Called when a fresh dream begins; the current run's row is created after."""
        cur = await self.conn.execute(
            "UPDATE dreams SET ended_at = started_at,"
            " summary = 'interrupted (process exited mid-dream)'"
            " WHERE ended_at IS NULL AND started_at < ?",
            (before,),
        )
        await self.conn.commit()
        return cur.rowcount

    async def decay_thoughts(self, *, rate: float, floor: float) -> int:
        """Nightly reflection: lower each thought's `prudence` (its current
        weight), slowed by `stability`, toward a floor — never to deletion. One
        set-based update. Returns rows touched."""
        # WHERE prudence > floor: floored rows are fixpoints (MAX(floor, …) can't move
        # them, and last_recompute_at has no reader), so skipping them leaves behavior
        # identical while avoiding a rewrite of *every* thought row each night.
        cur = await self.conn.execute(
            "UPDATE thoughts SET prudence = MAX(?, prudence * (1 - ? * (1 - stability))),"
            " last_recompute_at = ? WHERE prudence > ?",
            (floor, rate, utc_isoformat(), floor),
        )
        await self.conn.commit()
        return cur.rowcount

    async def decay_gap_salience(self, *, factor: float, floor: float) -> int:
        """Re-score open gaps downward so unattended curiosity fades (never below
        `floor`). Engagement/recency keep the salient ones near the top."""
        cur = await self.conn.execute(
            "UPDATE knowledge_gaps SET salience = MAX(?, salience * ?) WHERE status = 'open'",
            (floor, factor),
        )
        await self.conn.commit()
        return cur.rowcount

    async def open_merge_candidates(self) -> list[dict]:
        """Open duplicate-subject candidates for the dream to reconcile, with both
        names/kinds for context."""
        cur = await self.conn.execute(
            "SELECT mc.id, mc.a_id, mc.b_id, mc.score, mc.evidence,"
            " ea.name AS a_name, ea.kind AS a_kind, eb.name AS b_name, eb.kind AS b_kind"
            " FROM merge_candidates mc"
            " JOIN subjects ea ON ea.id = mc.a_id JOIN subjects eb ON eb.id = mc.b_id"
            " WHERE mc.status = 'open'"
        )
        return [dict(r) for r in await cur.fetchall()]

    async def set_merge_candidate_status(self, candidate_id: str, status: str) -> None:
        await self.conn.execute(
            "UPDATE merge_candidates SET status = ? WHERE id = ?", (status, candidate_id)
        )
        await self.conn.commit()

    async def merge_subjects(self, from_id: str, into_id: str, *, by: str, reason: str) -> None:
        """Fold `from_id` into `into_id`, non-destructively: re-point its
        attributes/relationships/aliases to the canonical subject, keep its name
        as an alias, and record an audited `subject_merge`. Nothing is deleted —
        the merge is reversible from the record."""
        if from_id == into_id:
            return
        dup = await self.get_subject(from_id)
        now = utc_isoformat()
        # Supersede the "these two are related" edges between them — folding them
        # in would make meaningless self-loops; keep them in history instead.
        await self.conn.execute(
            "UPDATE relationships SET valid_to = ? WHERE valid_to IS NULL"
            " AND ((from_id = ? AND to_id = ?) OR (from_id = ? AND to_id = ?))",
            (now, from_id, into_id, into_id, from_id),
        )
        # Re-point everything else to the canonical subject (the AND guards keep a
        # re-point from ever producing a self-loop).
        await self.conn.execute(
            "UPDATE attributes SET subject_id = ? WHERE subject_id = ?", (into_id, from_id)
        )
        # Observations are attributes' append-only twin and re-point with them.
        # OR IGNORE: if both subjects hold a datapoint at the same (key, observed_at)
        # it's the same reading at the same instant — keep the canonical one and leave
        # the duplicate on the folded (skipped) node, rather than abort the whole merge
        # on the UNIQUE(subject_id, key, observed_at) constraint.
        await self.conn.execute(
            "UPDATE OR IGNORE observations SET subject_id = ? WHERE subject_id = ?",
            (into_id, from_id),
        )
        await self.conn.execute(
            "UPDATE relationships SET from_id = ? WHERE from_id = ? AND to_id != ?",
            (into_id, from_id, into_id),
        )
        await self.conn.execute(
            "UPDATE relationships SET to_id = ? WHERE to_id = ? AND from_id != ?",
            (into_id, from_id, into_id),
        )
        await self.conn.execute(
            "UPDATE subject_aliases SET subject_id = ? WHERE subject_id = ?", (into_id, from_id)
        )
        # Carry Moko's thoughts about the folded node over to the canonical one,
        # so its inner life follows the merge (a duplicate ⇒ harmless re-show).
        await self.conn.execute(
            "UPDATE thought_links SET target_id = ?"
            " WHERE target_kind = 'subject' AND target_id = ?",
            (into_id, from_id),
        )
        # Re-point the remaining subject FKs so the fold is complete: the folded
        # node's conversation history, open curiosities, and held commitments all
        # follow it to the canonical subject. Without this they orphan on the dead
        # node — its messages vanish from `slice`, and a gap about it can never
        # auto-resolve (the answer now lives under into_id), so Moko keeps asking
        # what it already knows.
        await self.conn.execute(
            "UPDATE messages SET speaker_id = ? WHERE speaker_id = ?", (into_id, from_id)
        )
        await self.conn.execute(
            "UPDATE knowledge_gaps SET about_id = ? WHERE about_id = ?", (into_id, from_id)
        )
        await self.conn.execute(
            "UPDATE commitments SET about_id = ? WHERE about_id = ?", (into_id, from_id)
        )
        if dup is not None:
            await self.conn.execute(
                "INSERT INTO subject_aliases (id, subject_id, alias, source_id, created_at)"
                " VALUES (?, ?, ?, NULL, ?)",
                (new_id(), into_id, dup.name, now),
            )
        # Tag the folded subject (so lookups/scans skip it and new facts can't
        # re-attach), and drop its now-stale search rows so recall won't surface
        # the dead node. The row itself stays — the merge is reversible.
        await self.conn.execute(
            "UPDATE subjects SET merged_into = ? WHERE id = ?", (into_id, from_id)
        )
        await self._unindex(from_id)
        # into_id absorbed the folded node's name + aliases above; rebuild its search rows
        # so recall finds the survivor by any of the duplicate's names (see _reindex_subject).
        await self._reindex_subject(into_id)
        await self.conn.execute(
            "INSERT INTO subject_merges (id, from_id, into_id, at, by, reason)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (new_id(), from_id, into_id, now, by, reason),
        )
        await self.conn.commit()

    # ── approvals (the consent gate's memory) ────────────────────
    async def add_approval(self, action: str, kind: str) -> str:
        """Record a sensitive action awaiting the companion's approval. `action`
        is a JSON payload (tool + args + description); `kind` is its category."""
        aid = new_id()
        await self.conn.execute(
            "INSERT INTO approvals (id, action, kind, status, requested_at)"
            " VALUES (?, ?, ?, 'pending', ?)",
            (aid, action, kind, utc_isoformat()),
        )
        await self.conn.commit()
        return aid

    async def pending_approval(self) -> dict | None:
        """The most recent action still awaiting approval, if any."""
        cur = await self.conn.execute(
            "SELECT * FROM approvals WHERE status = 'pending'"
            " ORDER BY requested_at DESC LIMIT 1"
        )
        row = await cur.fetchone()
        return dict(row) if row else None

    async def set_approval_status(self, approval_id: str, status: str) -> None:
        """Move an approval along: approved/denied (decided) or executed."""
        stamp = "executed_at" if status == "executed" else "decided_at"
        await self.conn.execute(
            f"UPDATE approvals SET status = ?, {stamp} = ? WHERE id = ?",
            (status, utc_isoformat(), approval_id),
        )
        await self.conn.commit()

    # ── context assembly ──────────────────────────────────────────────
    async def slice(self, subject_ids: list[str], *, message_limit: int = 10) -> GraphSlice:
        """The relevant sub-graph: seeds + their current attrs + one hop
        of current relationships + neighbour subjects + recent messages."""
        seeds = list(subject_ids)
        if not seeds:
            return GraphSlice()

        subjects: dict[str, Subject] = {}
        for sid in seeds:
            subject = await self.get_subject(sid)
            if subject is not None:
                subjects[sid] = subject

        placeholders = ",".join("?" * len(seeds))
        cur = await self.conn.execute(
            f"SELECT * FROM relationships WHERE valid_to IS NULL"
            f" AND (from_id IN ({placeholders}) OR to_id IN ({placeholders}))",
            (*seeds, *seeds),
        )
        relationships = [Relationship.from_row(r) for r in await cur.fetchall()]

        neighbour_ids = [
            nid
            for rel in relationships
            for nid in (rel.from_id, rel.to_id)
            if nid not in subjects
        ]
        for subject in await self.get_subjects(neighbour_ids):  # one query, not N
            subjects[subject.id] = subject

        attributes = {sid: await self.attributes_for(sid) for sid in seeds}

        cur = await self.conn.execute(
            f"SELECT * FROM messages WHERE speaker_id IN ({placeholders})"
            f" ORDER BY sent_at DESC LIMIT ?",
            (*seeds, message_limit),
        )
        messages = [Message.from_row(r) for r in await cur.fetchall()]

        return GraphSlice(list(subjects.values()), relationships, attributes, messages)
