"""Typed records for the graph store.

Plain frozen dataclasses with a ``from_row`` constructor, so the rest of
the code works with attributes, not raw tuples.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import ulid


def new_id() -> str:
    """A fresh ULID — sortable and time-bearing."""
    return str(ulid.ULID())


def _opt(row: Any, key: str) -> Any:
    """Read a column a narrower SELECT (or a pre-migration row) may not carry, defaulting
    to None instead of raising — the single home for the optional-column contract.
    (Membership is tested against `row.keys()`, not `row`: `in` on a sqlite3.Row scans
    its *values*.)"""
    keys = row.keys()
    return row[key] if key in keys else None


@dataclass(frozen=True)
class Source:
    id: str
    kind: str
    ref: str
    channel: str | None
    excerpt: str | None
    observed_at: str
    created_at: str

    @classmethod
    def from_row(cls, row: Any) -> Source:
        return cls(
            id=row["id"],
            kind=row["kind"],
            ref=row["ref"],
            channel=row["channel"],
            excerpt=row["excerpt"],
            observed_at=row["observed_at"],
            created_at=row["created_at"],
        )


@dataclass(frozen=True)
class Subject:
    id: str
    kind: str
    name: str
    confidence: float
    notes: str
    created_at: str
    world_id: str | None = None  # M11: which world this subject belongs to (companion/moko)

    @classmethod
    def from_row(cls, row: Any) -> Subject:
        return cls(
            id=row["id"],
            kind=row["kind"],
            name=row["name"],
            confidence=row["confidence"],
            notes=row["notes"],
            created_at=row["created_at"],
            world_id=_opt(row, "world_id"),
        )


@dataclass(frozen=True)
class Attribute:
    id: str
    subject_id: str
    key: str
    value: str
    value_type: str
    valid_from: str | None
    valid_to: str | None
    source_id: str
    confidence: float
    created_at: str

    @classmethod
    def from_row(cls, row: Any) -> Attribute:
        return cls(
            id=row["id"],
            subject_id=row["subject_id"],
            key=row["key"],
            value=row["value"],
            value_type=row["value_type"],
            valid_from=row["valid_from"],
            valid_to=row["valid_to"],
            source_id=row["source_id"],
            confidence=row["confidence"],
            created_at=row["created_at"],
        )


@dataclass(frozen=True)
class Observation:
    """A time-anchored datapoint — the append-only twin of Attribute. Where an
    attribute is the current value of a property (supersede-on-change), an
    observation is one reading in a series, true at its ``observed_at`` instant
    forever; the series accumulates instead of overwriting."""

    id: str
    subject_id: str
    key: str
    value: str
    value_type: str
    observed_at: str
    source_id: str
    created_at: str

    @classmethod
    def from_row(cls, row: Any) -> Observation:
        return cls(
            id=row["id"],
            subject_id=row["subject_id"],
            key=row["key"],
            value=row["value"],
            value_type=row["value_type"],
            observed_at=row["observed_at"],
            source_id=row["source_id"],
            created_at=row["created_at"],
        )


@dataclass(frozen=True)
class Relationship:
    id: str
    from_id: str
    to_id: str
    type: str
    valid_from: str | None
    valid_to: str | None
    directed: bool
    source_id: str
    confidence: float
    created_at: str

    @classmethod
    def from_row(cls, row: Any) -> Relationship:
        return cls(
            id=row["id"],
            from_id=row["from_id"],
            to_id=row["to_id"],
            type=row["type"],
            valid_from=row["valid_from"],
            valid_to=row["valid_to"],
            directed=bool(row["directed"]),
            source_id=row["source_id"],
            confidence=row["confidence"],
            created_at=row["created_at"],
        )


@dataclass(frozen=True)
class Message:
    id: str
    channel: str
    speaker_id: str | None
    direction: str
    body: str
    sent_at: str
    sentiment: str | None
    valence: float | None
    arousal: float | None
    sentiment_confidence: float | None
    session_id: str | None
    meta: str | None
    created_at: str

    @classmethod
    def from_row(cls, row: Any) -> Message:
        return cls(
            id=row["id"],
            channel=row["channel"],
            speaker_id=row["speaker_id"],
            direction=row["direction"],
            body=row["body"],
            sent_at=row["sent_at"],
            sentiment=row["sentiment"],
            valence=row["valence"],
            arousal=row["arousal"],
            sentiment_confidence=row["sentiment_confidence"],
            session_id=row["session_id"],
            meta=row["meta"],
            created_at=row["created_at"],
        )


@dataclass(frozen=True)
class Session:
    id: str
    started_at: str
    ended_at: str | None
    started_reason: str
    ended_reason: str | None
    carryover: str | None
    token_estimate: int

    @classmethod
    def from_row(cls, row: Any) -> Session:
        return cls(
            id=row["id"],
            started_at=row["started_at"],
            ended_at=row["ended_at"],
            started_reason=row["started_reason"],
            ended_reason=row["ended_reason"],
            carryover=row["carryover"],
            token_estimate=row["token_estimate"],
        )


@dataclass(frozen=True)
class Thought:
    id: str
    kind: str
    body: str
    valence: float | None
    shareability: str
    prudence: float
    stability: float
    created_at: str
    world_id: str | None = None  # M11: which world the thought concerns (companion/moko)

    @classmethod
    def from_row(cls, row: Any) -> Thought:
        return cls(
            id=row["id"],
            kind=row["kind"],
            body=row["body"],
            valence=row["valence"],
            shareability=row["shareability"],
            prudence=row["prudence"],
            stability=row["stability"],
            created_at=row["created_at"],
            world_id=_opt(row, "world_id"),
        )


@dataclass(frozen=True)
class Gap:
    id: str
    about_id: str | None
    description: str
    kind: str
    resolution_path: str
    salience: float
    status: str
    opened_at: str
    world_id: str | None = None  # M11: which world the gap is about (companion/moko)

    @classmethod
    def from_row(cls, row: Any) -> Gap:
        return cls(
            id=row["id"],
            about_id=row["about_id"],
            description=row["description"],
            kind=row["kind"],
            resolution_path=row["resolution_path"],
            salience=row["salience"],
            status=row["status"],
            opened_at=row["opened_at"],
            world_id=_opt(row, "world_id"),
        )


@dataclass(frozen=True)
class Commitment:
    id: str
    description: str
    kind: str
    about_id: str | None
    due_at: str | None
    status: str
    created_at: str

    @classmethod
    def from_row(cls, row: Any) -> Commitment:
        return cls(
            id=row["id"],
            description=row["description"],
            kind=row["kind"],
            about_id=row["about_id"],
            due_at=row["due_at"],
            status=row["status"],
            created_at=row["created_at"],
        )


@dataclass(frozen=True)
class MediaArtifact:
    id: str
    source_id: str
    message_id: str | None
    kind: str  # image | audio | video | document
    mime: str | None
    sha256: str
    bytes: int
    path: str  # stored name, relative to ~/.moko/media/
    filename: str | None  # the sender's original filename, for display/recall
    caption: str | None  # the sender's caption
    description: str | None  # Moko's observation of what it saw (vision); the durable gist
    direction: str  # in | out
    created_at: str
    taken_at: str | None = None  # EXIF capture time (distinct from created_at = received)
    world_id: str | None = None  # M11: which world this artifact belongs to (companion/moko)

    @classmethod
    def from_row(cls, row: Any) -> MediaArtifact:
        return cls(
            id=row["id"],
            source_id=row["source_id"],
            message_id=row["message_id"],
            kind=row["kind"],
            mime=row["mime"],
            sha256=row["sha256"],
            bytes=row["bytes"],
            path=row["path"],
            filename=row["filename"],
            caption=row["caption"],
            description=row["description"],
            direction=row["direction"],
            created_at=row["created_at"],
            taken_at=_opt(row, "taken_at"),
            world_id=_opt(row, "world_id"),
        )


@dataclass(frozen=True)
class SearchHit:
    kind: str
    ref_id: str
    content: str
