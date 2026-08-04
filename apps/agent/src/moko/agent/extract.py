"""The shape of what Moko learns, and how to read it back.

Moko records knowledge by calling the `remember` tool inside its reply
loop (see `memory_tools.REMEMBER_TOOL`); its arguments are an object of
subjects, typed attributes, relationships, the speaker's perceived
sentiment, private thoughts, fresh knowledge gaps, and commitments it has
agreed to. `Extraction` is
that payload's typed form; `extraction_from_obj` normalises a raw
arguments dict into it. Reading is best-effort and must never raise — bad
input becomes an empty `Extraction`, never a broken turn.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Extraction:
    subjects: list[dict] = field(default_factory=list)
    attributes: list[dict] = field(default_factory=list)
    observations: list[dict] = field(default_factory=list)
    relationships: list[dict] = field(default_factory=list)
    speaker_sentiment: dict | None = None
    thoughts: list[dict] = field(default_factory=list)
    gaps: list[dict] = field(default_factory=list)
    commitments: list[dict] = field(default_factory=list)
    # The numbers of the open questions (shown to the model) the companion just
    # answered — the model closes them in the same turn it's already taking.
    resolved_gaps: list[int] = field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        """Nothing to record — a no-op payload (e.g. a forced `remember` call on a
        turn that revealed nothing). Callers skip it so it never writes an orphan
        source row with no facts attached, nor counts as a turn that 'recorded'."""
        return not (
            self.subjects
            or self.attributes
            or self.observations
            or self.relationships
            or self.thoughts
            or self.gaps
            or self.commitments
            or self.resolved_gaps
            or self.speaker_sentiment
        )


def extraction_from_obj(data) -> Extraction:
    """Normalise a raw arguments object (e.g. a `remember` tool call) into an
    `Extraction`. Never raises — anything unexpected becomes empty fields."""
    if not isinstance(data, dict):
        return Extraction()

    def as_list(value) -> list[dict]:
        return [x for x in value if isinstance(x, dict)] if isinstance(value, list) else []

    def as_ints(value) -> list[int]:
        items = value if isinstance(value, list) else []
        return [  # the model may emit 2 or "2"
            int(n)
            for n in items
            if isinstance(n, int) or (isinstance(n, str) and n.strip().lstrip("-").isdigit())
        ]

    sentiment = data.get("speaker_sentiment")
    if not isinstance(sentiment, dict):
        sentiment = None

    return Extraction(
        subjects=as_list(data.get("subjects")),
        attributes=as_list(data.get("attributes")),
        observations=as_list(data.get("observations")),
        relationships=as_list(data.get("relationships")),
        speaker_sentiment=sentiment,
        thoughts=as_list(data.get("thoughts")),
        gaps=as_list(data.get("gaps")),
        commitments=as_list(data.get("commitments")),
        resolved_gaps=as_ints(data.get("resolved_gaps")),
    )
