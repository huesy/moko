"""The brain — perceive → assemble context → respond, remembering as it goes.

One turn:
  1. pick the session (rotating on idle);
  2. record the inbound message;
  3. assemble a context slice from the graph + recent session messages;
  4. ask the LLM for a reply — in one tool-use loop it may `recall`/`lookup`
     to ground itself and `remember` what the companion just revealed;
  5. record the reply;
  6. update the session's size and rotate if it's grown too large.

Writing is folded into the reply loop (the `remember` tool), so a turn is a
single LLM interaction — no separate extraction call. Knowledge is still
consolidated every turn, so rotating a session only ever drops the verbatim
transcript, never what Moko learned.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import json
import mimetypes
import re
from dataclasses import dataclass, field, replace
from pathlib import Path

from ..clock import Clock, now_utc
from ..config.config import Config
from ..config.paths import media_dir, skills_dir
from ..config.policy import load_policy, set_policy
from ..events import publish as _publish_event
from ..llm import ImagePart, LLMClient, Msg, Tool, estimate_tokens
from ..log import get_logger, log_exchange
from ..skills import load_skills
from ..store import COMPANION_WORLD, MOKO_WORLD, GraphSlice, Store, blobs, exif
from ..tools import ALL_TOOLS, AgentTool, weather
from ..tools.base import SENSITIVE, GateDecision
from ..tools.consent import ALLOW, ASK, DENY
from ..tools.consent import decide as consent_decide
from ..tools.shell import FREEABLE_KEYS, GATE
from .curiosity import detect_gaps_for
from .extract import Extraction, extraction_from_obj
from .media_studio import MediaStudio
from .memory_tools import MEMORY_TOOLS, REMEMBER_TOOL, make_executor
from .persona import _feeling_word, system_prompt
from .proactivity import OUTREACH_BUDGET_WINDOW_HOURS, ProactivityGovernor
from .rhythms import (
    REFLECT_DRY_KEY,
    REFLECT_LAST_KEY,
    REFLECT_SEEN_KEY,
    reflection_gate,
)
from .usage import UsageMeter, usage_kind

# Subject kinds that belong to someone — used to ask "whose is this?" when an
# extraction records one without linking it to anyone.
_POSSESSABLE_KINDS = {"pet", "object"}

# Calm vs keyed-up boundary on the arousal axis (0..1): at/above this a state
# reads as "keyed-up" rather than flat. One source of truth, shared by the
# situation-header wording (_arousal_word) and the well-being trend below.
AROUSAL_KEYED_UP = 0.65

# The well-being check-in signal, drawn from message sentiment. A confidence-
# weighted average valence at or below the threshold reads as a low-mood trend
# worth a gentle check-in (valence runs -1..1), at most one per window so it's
# never naggy. Two guards keep it honest:
#   • Scope — only calm-to-flat reads count. The check-in's voice is "you've
#     seemed low/flat lately", which fits a withdrawn low mood, not a keyed-up
#     state (anger, agitation, and — a deliberate scoping call — acute distress);
#     a keyed-up moment is the live reply's to meet, not this slow trend's.
#     Persistent keyed-up distress is a known gap for a future dedicated path.
#   • Confidence — each read is weighted by its confidence, so an unsure read
#     can't drive a governed outreach on its own (the mission's "sentiment is a
#     hypothesis, held gently"). A read that didn't flag itself unsure counts as
#     normal-confidence; only an explicitly low confidence down-weights. This
#     makes sentiment_confidence load-bearing rather than write-only.
MOOD_LOW_VALENCE = -0.3
WELLBEING_VALENCE_SAMPLES = 8  # recent messages read for the mood trend
WELLBEING_MIN_SAMPLES = 3  # minimum confidence-weighted signal mass before a trend is trusted
WELLBEING_DEFAULT_CONFIDENCE = 0.8  # weight for a read that didn't flag itself unsure
WELLBEING_CHECKIN_WINDOW_HOURS = 24  # at most one check-in per this window

# Working-memory (session) shape. A session is a rolling context window; it resets
# after a long idle gap or once it grows past the token budget.
SESSION_IDLE_RESET_HOURS = 6.0  # a gap this long since the last message starts a fresh session
SESSION_MAX_TOKENS = 6000  # session rotates once it grows past this
SESSION_HISTORY_MESSAGES = 12  # verbatim recent messages re-sent each turn — keep lean

# Idle musing (reflection) records thoughts/gaps with only the `remember` tool, so skills
# about surfaces it doesn't have (speaking style, web tools, world/photo tools) are dead
# weight there — this fixed subset keeps each reflection ~1k tokens lighter AND byte-stable,
# so clustered reflections hit their own prompt cache.
_MUSING_SKILLS = {"curiosity", "honesty", "inner-life", "remembering"}

# Tool-use round-trips allowed in one reply. A plain chat turn uses 1-2; an action
# turn ("build a site, run it, check it") needs room — each shell command is one
# step. High enough that real multi-step work finishes, bounded so a confused loop
# can't run away. When it's hit, the loop still produces an honest closing reply
# (see the LLM adapter), never dead air.
REPLY_MAX_STEPS = 16

# How many of Moko's open questions to surface in a reply turn — enough that the
# relevant ones are present for the model to weave in or mark answered, capped so
# the prompt stays lean and Moko never interrogates.
_OPEN_QUESTIONS_SHOWN = 5

# A commitment whose parsed due time is already this far in the past is almost
# certainly a hallucinated date, so it's demoted to an undated "someday" rather
# than firing the instant it's recorded. Defensive parsing, not a user knob.
COMMITMENT_PAST_DUE_GRACE_HOURS = 2

# Moko is the `self` subject; extraction must never re-create it as a node.
_RESERVED_SUBJECT_NAMES = {"moko"}
# Bare relational role-words are not names — "my brother" must not become an
# subject called "brother". A backstop for when the model ignores the
# `remembering` skill's guidance; kept to family/role terms that are
# essentially never real first names or pet names (so few false positives).
# The skill guidance is the primary, language-agnostic fix; this is English.
_GENERIC_ROLES = {
    "mum", "mom", "mother", "dad", "father", "parent", "brother", "sister",
    "sibling", "grandma", "grandmother", "grandpa", "grandfather", "granddad",
    "aunt", "uncle", "cousin", "nephew", "niece", "husband", "wife", "spouse",
    "partner", "boss", "colleague", "coworker", "co-worker", "neighbour",
    "neighbor", "landlord",
}
# Single-valued ("functional") relationship types: a person lives in one place,
# works at one employer, at a time. A new target for one of these supersedes the
# old (with history) and opens a verification gap — vs. multi-valued edges like
# friend_of / sibling_of, where many are true at once.
_FUNCTIONAL_RELS = {
    "lives_in", "works_at", "works_in", "located_in", "based_in", "born_in",
    "employed_by", "resides_in", "lives_with", "studies_at", "attends",
}
# Sources where the companion told Moko directly — authoritative, so a change
# they state needs no "verify with them" gap (they just said it).
_COMPANION_SOURCES = {"message", "user_stated"}
# Substrings that make an attribute key UNAMBIGUOUSLY geographic — its value is
# a named place, never anything else. Deliberately conservative (no "state",
# "region", "location" — those can be non-geographic; the dream's LLM
# reclassification settles ambiguous ones using world knowledge). No
# capitalization is used: "city=tokyo" converts just like "City=Tokyo".
_UNAMBIGUOUS_GEO_TERMS = (
    "country", "nation", "continent", "province", "county", "city", "town",
)


def _attr_relation(key: str) -> str:
    """The relationship type when an attribute's value is actually a subject —
    geographic keys get clean semantics, others reuse the key."""
    if "capital" in key:
        return "has_capital"
    if any(term in key for term in _UNAMBIGUOUS_GEO_TERMS):
        return "located_in"
    return key


def _split_places(value: str) -> list[str]:
    """Split a compound place value ('Victoria, Australia') into its parts. But a
    value with a parenthetical ('Zargon (game master / villain)') is one descriptor,
    NOT a compound — never split it, or the `/` and `,` shatter a name into junk
    subjects ('Zargon (game master', 'villain)')."""
    if "(" in value or ")" in value:
        v = value.strip()
        return [v] if v else []
    return [p.strip() for p in re.split(r",| and |&|/", value) if p.strip()]


_DESCRIPTOR_TAIL = re.compile(r"\s*\([^)]*\)\s*$")


def _clean_subject_name(name: str) -> str:
    """Keep subject *names* clean proper nouns. Strip a trailing descriptor —
    'Zargon (game master)' → 'Zargon' — and reject a fragment with leftover
    unbalanced parens ('Zargon (game master', 'villain)') by returning '', so a
    mangled value is skipped rather than minted as junk that then defeats recall."""
    n = _DESCRIPTOR_TAIL.sub("", name.strip()).strip()
    return "" if ("(" in n or ")" in n) else n
# Values that mean "I don't actually know" — store these as gaps, not facts.
_VAGUE_VALUES = ("unspecified", "unknown", "not specified", "n/a", "tbd", "somewhere")
# A standard English function-word stop list (articles, prepositions,
# conjunctions, pronouns, auxiliaries, question words). Deliberately generic —
# NOT tuned to any observed data, and never content words. Used only to compare
# gap descriptions so near-identical questions dedup on their meaningful words.
_GAP_STOPWORDS = {
    "a",
    "an",
    "the",
    "and",
    "or",
    "but",
    "if",
    "of",
    "at",
    "by",
    "for",
    "with",
    "about",
    "to",
    "from",
    "in",
    "into",
    "on",
    "onto",
    "off",
    "over",
    "under",
    "as",
    "is",
    "are",
    "was",
    "were",
    "be",
    "been",
    "being",
    "am",
    "have",
    "has",
    "had",
    "do",
    "does",
    "did",
    "will",
    "would",
    "can",
    "could",
    "should",
    "may",
    "might",
    "must",
    "this",
    "that",
    "these",
    "those",
    "i",
    "you",
    "he",
    "she",
    "it",
    "we",
    "they",
    "me",
    "him",
    "her",
    "us",
    "them",
    "my",
    "your",
    "his",
    "its",
    "our",
    "their",
    "mine",
    "yours",
    "what",
    "which",
    "who",
    "whom",
    "whose",
    "when",
    "where",
    "why",
    "how",
    "there",
    "here",
    "then",
    "than",
    "so",
    "such",
    "no",
    "not",
    "only",
    "some",
    "any",
    "all",
    "more",
    "most",
    "other",
    "just",
    "also",
}


def _as_float(value) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _as_valence(value) -> float | None:
    """A valence, clamped to its real range (−1..1). The model can emit anything;
    an out-of-range value would skew decay (stability) and the well-being fuse."""
    v = _as_float(value)
    return None if v is None else max(-1.0, min(1.0, v))


def _as_unit(value) -> float | None:
    """A 0..1 quantity (confidence, arousal), clamped to range on the way in."""
    v = _as_float(value)
    return None if v is None else max(0.0, min(1.0, v))


# Flashbulb effect: a thought Moko feels strongly about (high |valence|) starts
# with higher stability, so it decays slower and stays prominent. A neutral
# thought (no valence) keeps the store's default — `_stability_from_valence(None)`
# returns exactly that, so callers can pass it unconditionally.
_STABILITY_BASE = 0.5
_STABILITY_VALENCE_GAIN = 0.4  # |valence|→1 reaches ~0.9


def _stability_from_valence(valence: float | None) -> float:
    if valence is None:
        return _STABILITY_BASE
    return max(0.0, min(1.0, _STABILITY_BASE + _STABILITY_VALENCE_GAIN * abs(valence)))


# Idle-reflection cadence + its app_state keys live in agent/rhythms.py — the
# single source of truth shared with the monitor's rhythm bar, so the gating here
# and what the UI shows can't drift. (`reflect_last_at` = when Moko last actually
# reflected; `reflect_seen_at` = the last message it reflected on, for fresh-
# material detection; `reflect_dry` = consecutive empty musings, for backoff.)

# app_state key: the brain-decided instant (UTC iso) before which Moko won't
# re-consult itself about reaching out — so a deferred outreach doesn't re-ask
# the LLM every tick. A due commitment can still break through it.
RECONSIDER_KEY = "outreach_reconsider_at"

# Bounds on the brain's model-chosen "reconsider in N minutes" so a stray value can't
# cause tight LLM polling (the minimum) or effectively silence Moko for days (the
# maximum). The default is used when the model names no time.
OUTREACH_RECONSIDER_MIN_MINUTES = 10
OUTREACH_RECONSIDER_DEFAULT_MINUTES = 30
OUTREACH_RECONSIDER_MAX_MINUTES = 72 * 60  # ~3 days — the anti-silence backstop

# The brain's outreach decision, made by consulting itself rather than a fixed
# schedule. `terminal` so the consult is a single round-trip.
DECIDE_OUTREACH_TOOL = Tool(
    name="decide_outreach",
    description=(
        "Decide, as yourself, whether to reach out to the companion right now and with what. "
        "Reach out only if it's genuinely a good moment and you have something worth saying."
    ),
    terminal=True,
    parameters={
        "type": "object",
        "properties": {
            "act": {"type": "boolean", "description": "reach out right now?"},
            "candidate": {
                "type": "string",
                "description": "if acting, the [token] of the one thing to reach out about",
            },
            "message": {
                "type": "string",
                "description": "if acting, the exact message to send, in your own voice",
            },
            "photo_subject": {
                "type": "string",
                "description": (
                    "ONLY for the [show] candidate: what to picture, to send with your "
                    "message — a selfie pose / what you're doing, or a thing in your world "
                    "(described vividly). Leave empty for a plain selfie."
                ),
            },
            "photo_is_selfie": {
                "type": "boolean",
                "description": (
                    "ONLY for [show]: true if the picture is of YOU (a selfie / you doing "
                    "something), false if it's of a thing in your world."
                ),
            },
            "reconsider_in_minutes": {
                "type": "number",
                "description": (
                    "when to consider reaching out again, in MINUTES from now "
                    "(e.g. 15 = shortly, 180 = a few hours, ~1080 = tomorrow morning). "
                    "Used whether or not you act."
                ),
            },
            "reasoning": {"type": "string", "description": "a brief private note on why"},
        },
        "required": ["act"],
    },
)


@dataclass
class OutreachProposal:
    """A vetted decision to reach out. `topic` is the internal marker/description;
    the wording is the brain's (it phrases the message during the consult).
    `reconsider_in_minutes` is when Moko should next consider reaching out."""

    kind: str  # "commitment" | "curiosity" | "wellbeing" | "reconnect" | "show"
    topic: str
    counts_to_budget: bool
    gap_id: str | None = None
    commitment_ids: list[str] = field(default_factory=list)  # all the reminders this covers
    reconsider_in_minutes: float | None = None
    # A photo (relative path under ~/.moko/media/) to send *with* the message — set only
    # for a "show" outreach, where Moko shares a picture of something from its world.
    photo_path: str | None = None


@dataclass
class Turn:
    """A completed reply. `recorded` is whether the model called `remember`
    this turn; the token fields carry the turn's usage for callers that want
    to surface it (e.g. `moko chat`'s verbose stats footer)."""

    reply: str
    recorded: bool = False
    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_read_tokens: int | None = None


def _relative_gap(seconds: float) -> str:
    """A coarse human gap ('20m', '4h', '3 days') for the situation header."""
    minutes = seconds / 60
    if minutes < 60:
        return f"{max(1, int(minutes))}m"
    hours = minutes / 60
    if hours < 36:
        return f"{int(round(hours))}h"
    return f"{int(round(hours / 24))} days"


def _attachment_perception(artifacts) -> str:
    """The fallback note for a turn whose attachments Moko *can't* see — a model with
    no vision, or a non-image file (audio/doc, which have no understanding wired up
    yet). It learns a file arrived, framed honestly as not-yet-viewable, so it
    acknowledges without pretending to have seen inside. (When the model can see an
    image, `_vision_perception` is used instead and the bytes ride into the turn.)"""
    listing = "\n".join(f"- {a.kind}: {a.filename or a.kind}" for a in artifacts)
    return (
        "\n\n[The companion attached the following with this message:\n"
        f"{listing}\n"
        "You received and saved them, but can't see or open their contents yet. "
        "Acknowledge them honestly — don't pretend to have seen inside — and ask "
        "about them if it's natural.]"
    )


def _vision_perception(images, others) -> str:
    """The note for a turn where Moko *can* see (a vision-capable model + image[s]
    attached below). It can describe what's actually there — and the honesty
    boundary still holds: any non-image attachment is named as not-yet-openable."""
    lines = []
    if images:
        n = len(images)
        lines.append(
            f"[You can see {n} image{'s' if n != 1 else ''} the companion just sent — "
            "attached below. Look, and respond to what's actually in them. In your "
            "`remember` call, set `saw` to a brief description of what's in the image so "
            "you'll remember this photo later; record any real facts too (from a photo, "
            "so hold them as something you saw, not as their word).]"
        )
    if others:
        named = ", ".join(f"{a.kind}: {a.filename or a.kind}" for a in others)
        lines.append(f"[Also attached, which you can't open yet: {named}.]")
    return ("\n\n" + "\n".join(lines)) if lines else ""


def _heard_perception(transcripts: list[str]) -> str:
    """Surface what Moko *heard* in inbound voice notes — it genuinely listened (the
    audio was transcribed by its hearing model), so it answers the words, not just the
    arrival of a file. The honesty boundary holds the other way too: a fact heard this
    way is held as 'something I heard', not 'their word' verbatim."""
    body = "\n".join(f'- "{t}"' for t in transcripts)
    return (
        "\n\n[You listened to the voice note(s) the companion just sent — transcript:\n"
        f"{body}\n"
        "Respond to what they actually said. Anything you `remember` from this, hold as "
        "something you heard.]"
    )


def _loggable_content(content):
    """Make a message's content JSON-safe for the exchange log — image bytes never
    serialize, so an `ImagePart` becomes a compact placeholder."""
    if isinstance(content, str):
        return content
    return [
        f"<image {p.mime} {len(p.data)}B>" if isinstance(p, ImagePart) else p
        for p in content
    ]


_MAX_SEND_BYTES = 50 * 1024 * 1024  # refuse oversize rather than 413 at the channel

_KIND_BY_EXT = {
    ".jpg": "image", ".jpeg": "image", ".png": "image", ".webp": "image", ".gif": "image",
    ".ogg": "audio", ".mp3": "audio", ".m4a": "audio", ".wav": "audio",
    ".mp4": "video", ".mov": "video", ".webm": "video",
}


def _media_kind_for(path: Path) -> str:
    """image | audio | video | document, from a file's extension."""
    return _KIND_BY_EXT.get(path.suffix.lower(), "document")


SEND_ATTACHMENT_TOOL = Tool(
    name="send_attachment",
    description=(
        "Send a file you have on disk to the companion in this chat — something they "
        "sent you, or that you've made or saved. Give its `path` (and an optional "
        "`caption`). A file under your own media folder goes straight through; a file "
        "anywhere else on disk asks the companion first. The caption is what they read "
        "WITH the file — so don't repeat it in your reply afterward; either let the "
        "captioned file stand on its own, or follow up with something genuinely new."
    ),
    parameters={
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "path to the file to send"},
            "caption": {"type": "string", "description": "an optional caption"},
        },
        "required": ["path"],
    },
)


def _arousal_word(arousal: float | None) -> str:
    """Calm vs keyed-up — the second affect axis, orthogonal to valence (low and
    flat wants space; low and agitated wants grounding). '' when middling/unknown."""
    if arousal is None:
        return ""
    if arousal >= AROUSAL_KEYED_UP:
        return ", keyed-up"
    if arousal <= 0.3:
        return ", flat"
    return ""


def _companion_mood(messages) -> str | None:
    """The companion's current mood for the situation header: their most recent
    perceived emotion plus the recent valence trend and arousal (e.g. 'frustrated
    (-0.4, keyed-up)') — so Moko opens the turn already reading the room."""
    inbound = [m for m in messages if m.direction == "in"]
    label = next((m.sentiment for m in reversed(inbound) if m.sentiment), None)
    arousal = next((m.arousal for m in reversed(inbound) if m.arousal is not None), None)
    vals = [m.valence for m in inbound if m.valence is not None]
    avg = sum(vals) / len(vals) if vals else None
    if label and avg is not None:
        return f"{label} ({avg:+.1f}{_arousal_word(arousal)})"
    if label:
        return label
    return f"{_feeling_word(avg)} ({avg:+.1f})" if avg is not None else None


def _keywords(text: str) -> set[str]:
    words = re.findall(r"[a-z]+", text.lower())
    return {w for w in words if len(w) > 2 and w not in _GAP_STOPWORDS}


def _similar_gap(a: str, b: str, *, threshold: float = 0.5) -> bool:
    """Cheap lexical overlap so we don't pile up rephrasings of one question.

    (Semantic dedup is the dream process's job in M6; this just stops the
    obvious "name of the testing server" x4 spam at write time.)
    """
    ka, kb = _keywords(a), _keywords(b)
    if not ka or not kb:
        return False
    return len(ka & kb) / len(ka | kb) >= threshold


def _echoes_a_caption(reply: str, captions: list[str]) -> bool:
    """True when the turn's reply is essentially a restatement of a caption already sent
    on an attachment this turn — so we drop the reply and let the captioned file stand
    alone (the companion shouldn't read the same line twice). A high overlap bar, so a
    reply that adds genuinely new content is kept."""
    rk = _keywords(reply)
    if not rk:
        return False
    for cap in captions:
        ck = _keywords(cap)
        if not ck:
            continue
        mutual = len(rk & ck) / len(rk | ck) >= 0.6  # strong two-way overlap
        # The looser "reply adds nothing new" test (≥80% of its words are in the caption)
        # only for replies long enough to judge — else a 1-2 word "Done!" trivially trips it.
        subset = len(rk) > 3 and len(rk & ck) / len(rk) >= 0.8
        if mutual or subset:
            return True
    return False


def _describe_action(name: str, args: dict) -> str:
    """A short, human-readable description of a sensitive action, for the
    approval request the companion sees."""
    if name == "execute_command":
        return f"run the command: `{args.get('command', '')}`"
    if name == "write_file":
        return f"write to {args.get('path', '?')}"
    if name == "author_skill":
        return f"teach myself a new skill: {args.get('name', '?')}"
    if name == "send_attachment":
        return f"send the file {args.get('path', '?')} to you"
    if name == "generate_image":
        return f"generate an image: {args.get('prompt', '?')}"
    if name == "generate_video":
        return f"generate a video: {args.get('prompt', '?')}"
    if name == "speak":
        spoken = str(args.get("text", "")).strip()
        return f"say aloud: {spoken[:80]}" + ("…" if len(spoken) > 80 else "")
    if name == "self_portrait":
        scene = args.get("scenario")
        return f"make a picture of myself{f' ({scene})' if scene else ''}"
    if name == "share_photo":
        return f"take a photo of {args.get('subject', '?')} to show you"
    detail = ", ".join(f"{k}={v}" for k, v in args.items() if k not in {"content", "instructions"})
    return name.replace("_", " ") + (f" ({detail})" if detail else "")


def _action_signature(name: str, args: dict) -> str:
    """A stable fingerprint of a tool call, so an action the companion declined (or
    moved on from) this turn is recognized if the model re-proposes it verbatim and
    isn't re-prompted — 'no means no', deterministically."""
    return json.dumps({"tool": name, "args": args}, sort_keys=True, default=str)


def _looks_informative(text: str) -> bool:
    """Rough flag: the companion probably revealed something worth recording.

    Observability only — it flags a turn that learned nothing despite looking
    substantive; it never triggers extra LLM work. A few content words is a
    good-enough proxy ("ok thanks" → no; "Maya moved to Tokyo" → yes)."""
    return len(_keywords(text)) >= 3


class Brain:
    def __init__(
        self,
        store: Store,
        llm: LLMClient,
        config: Config,
        *,
        session_max_tokens: int = SESSION_MAX_TOKENS,
        session_idle_reset_hours: float = SESSION_IDLE_RESET_HOURS,
        session_history_messages: int = SESSION_HISTORY_MESSAGES,
        tools: list[AgentTool] | None = None,
    ) -> None:
        self.store = store
        # Meter every brain-model call (reply, vision, reflection, dream, world, outreach,
        # research) into the usage ledger. Wrapping here means the studio + world model,
        # which get this same instance, are metered too — one seam, total coverage.
        self.llm = UsageMeter(llm, store)
        self.config = config
        self.session_max_tokens = session_max_tokens
        self.session_idle_reset_hours = session_idle_reset_hours
        self.session_history_messages = session_history_messages
        self.clock = Clock(config.timezone)
        # Skills (built-in + the companion's own) are injected into the cached prefix — the
        # prompt is stable across a session. Reloaded on session rotation (`_reload_skills`,
        # driven off the session-id change in `respond`) so a skill Moko authors mid-session
        # goes live in the next conversation — without any per-turn file I/O.
        self._skills_session: str | None = None
        self._reload_skills()
        # Action tools (web, file, system), dispatched through the consent gate.
        self._tools = {t.spec.name: t for t in (ALL_TOOLS if tools is None else tools)}
        # Outbound attachments are a brain-bound tool: it needs the per-turn channel
        # send path and the store, so it closes over `self` rather than being a static
        # module tool. Gated so a file outside Moko's own media folder asks first.
        send_tool = self._make_send_attachment_tool()
        self._tools.setdefault(send_tool.spec.name, send_tool)
        # Generative media (image/video/speech/hearing) + the world model live in the
        # studio, separate from the brain. It builds its (optional) clients once and offers
        # only the tools whose client is configured; an absent one means the capability is
        # honestly off. Hearing (STT) is perception, not a tool — see the attachment path.
        self.studio = MediaStudio(store, config, self.llm, clock=self.clock)
        for tool in self.studio.tools():
            self._tools.setdefault(tool.spec.name, tool)
        self._sent_captions: list[str] = []  # attachment captions sent this turn (no echo)
        self._sent_attachment = False  # did any file go out this turn (caption or not)?
        self._tool_policy = load_policy()  # per-category trust, persisted under ~/.moko
        # Injected per-turn by the gateway: awaits the companion's decision on a
        # sensitive action (payload, description, category, always_ok) →
        # "approve"/"always"/"deny"/"redirect"/"timeout". None when no approver is
        # wired (e.g. tests, autonomous paths).
        self._request_approval = None
        # Injected per-turn by the gateway when the channel can send files: hands
        # (data, filename, caption, kind) to the channel. None on channels/paths that
        # can't (the CLI, autonomous beats) → `send_attachment` degrades honestly.
        self._send_file = None
        self._reply_channel = "cli"
        # Actions declined or moved-on-from this turn — re-proposals don't re-ask.
        self._abandoned: set[str] = set()
        self._companion_id: str | None = None
        self._moko_id: str | None = None

    async def _ensure_actors(self) -> tuple[str, str]:
        if self._companion_id is None:
            companion = await self.store.add_subject(
                "person", self.config.companion_name or "the companion",
                world_id=COMPANION_WORLD,
            )
            self._companion_id = companion.id
        if self._moko_id is None:
            # Moko itself lives in Moko's world, not the companion's.
            self._moko_id = (
                await self.store.add_subject("self", "Moko", world_id=MOKO_WORLD)
            ).id
        return self._companion_id, self._moko_id

    def _reload_skills(self) -> None:
        """Re-read skills from disk (built-in + the companion's own in ~/.moko/skills). Called
        on session rotation via `_session_skills_check`, so a skill Moko authored mid-session
        becomes live in the next conversation — the honest meaning of `author_skill`'s "from our
        next conversation onward". Only fires on rotation, keeping the per-turn hot path free of
        file I/O and the cached prefix byte-stable within a session."""
        self.skills = load_skills(skills_dir())
        self._musing_skills = [s for s in self.skills if s.name in _MUSING_SKILLS]

    def _session_skills_check(self, session_id: str) -> None:
        """Reload skills whenever the active session changed since the last check — covers every
        rotation path (idle, size, /new, post-dream) with one cheap comparison."""
        if session_id != self._skills_session:
            self._skills_session = session_id
            self._reload_skills()

    async def new_session(self, reason: str = "user"):
        """Start a fresh working-memory session (the '/new' command)."""
        return await self.store.start_session(reason)

    async def _session_for_turn(self):
        session = await self.store.ensure_session()
        last = await self.store.last_message_at(session_id=session.id)
        if last is not None:
            gap = now_utc() - dt.datetime.fromisoformat(last)
            if gap > dt.timedelta(hours=self.session_idle_reset_hours):
                session = await self.store.start_session("idle")
        self._session_skills_check(session.id)  # pick up a newly authored skill on rotation
        return session

    async def respond(
        self, user_text: str, *, channel: str = "cli", on_event=None, request_approval=None,
        attachments=None, send_file=None,
    ) -> Turn:
        """One full turn: perceive → reply (recalling/remembering inline) →
        persist. A single LLM interaction; the `remember` tool writes any
        knowledge into the graph as part of the reply loop.

        `on_event`, if given, is called with a dict for each tool call —
        ``{"type": "tool", "tool", "args", "result"}`` — so a UI can show the
        turn's tool use live (used by `moko chat`'s verbose mode). Best-effort:
        a failing callback never breaks the turn."""
        companion_id, moko_id = await self._ensure_actors()
        session = await self._session_for_turn()

        inbound = await self.store.add_message(
            channel, "in", user_text, speaker_id=companion_id, session_id=session.id
        )

        # Files that rode in with the message: persist each as a *sourced* artifact
        # (bytes to ~/.moko/media/, a provenance row) linked to this message. Moko
        # perceives that they arrived even though it can't see inside them yet — the
        # honesty boundary: acknowledge, don't pretend to have seen (vision is M10's
        # next slice). A failed save must not sink the turn.
        stored_media = []
        exif_notes: list[str] = []  # where/when an image was taken, for the perception note
        heard: list[str] = []  # transcripts of inbound voice notes Moko could listen to
        for att in attachments or []:
            # Read EXIF off inbound images: keep the *capture* time (Moko is time-aware),
            # and surface GPS for this turn without persisting a location log.
            meta = exif.read_exif(att.data, att.mime) if att.kind == "image" else {}
            # Hearing: transcribe an inbound voice note so Moko genuinely *hears* it (the
            # companion's own audio → free, no gate). The transcript becomes the artifact's
            # description and rides into the turn; a failed listen degrades to the honest
            # "a voice note arrived" note. None when no hearing model is configured.
            transcript = await self.studio.transcribe(att)
            try:
                art = await self.store.add_media(
                    att.data, kind=att.kind, mime=att.mime, filename=att.filename,
                    caption=att.caption, description=transcript, taken_at=meta.get("taken_at"),
                    message_id=inbound.id, channel=channel, ref=att.ref,
                )
                stored_media.append(art)
            except Exception:
                get_logger("brain").warning("couldn't store an inbound attachment", exc_info=True)
                continue
            # Surface the received file on the feed (with a thumbnail deep-link).
            _publish_event({"type": "media.received", "id": art.id, "media_kind": art.kind,
                            "filename": att.filename, "caption": att.caption})
            if transcript:
                heard.append(transcript)
            bits = []
            if meta.get("taken_at"):
                bits.append(f"taken {meta['taken_at']}")
            if "lat" in meta and "lon" in meta:
                # Note that it's geotagged, but NOT the raw coordinates — surfacing
                # precise location invites Moko to echo or persist a location log; the
                # capture time is the genuinely useful, low-sensitivity signal.
                bits.append("has location data")
            if bits:
                exif_notes.append("(" + "; ".join(bits) + ")")

        # Orientation core: just the companion and their immediate world, and
        # the persona caps how much of it renders. Anything deeper, Moko fetches
        # itself via the recall/lookup tools — no eager slice guessing what's
        # relevant. (message_limit=0: subjects/attrs/rels only; history below.)
        graph = await self.store.slice([companion_id], message_limit=0)
        # Curiosity (Phase C): offer one open ask-path gap for Moko to weave in
        # if the conversation opens to it. Surfaced as a gentle nudge, not a
        # command; the model phrases the question itself, in the companion's
        # language. Marked surfaced after the turn so questions rotate and pace.
        # Prefer a gap *about* someone/something in the companion's immediate world
        # (the slice), so a stored question gets woven into the conversation it's
        # relevant to — talking about the dog surfaces the dog-name gap — rather
        # than a top-salience gap about someone not in play. Fall back to the best
        # open gap when nothing topical is queued.
        slice_ids = {e.id for e in graph.subjects}
        # The open questions relevant to what's in play, numbered so the model can
        # weave one in AND mark any the companion just answered (`resolved_gaps`) —
        # semantic resolution rides the reply turn, no extra call. Prefer gaps about
        # an in-play subject, then fill with the most salient open ask/either gaps.
        _askable = [
            g for g in await self.store.open_gaps(limit=30)
            if g.resolution_path in ("ask", "either")
        ]
        shown_gaps = [g for g in _askable if g.about_id in slice_ids]
        shown_gaps += [g for g in _askable if g.about_id not in slice_ids]
        shown_gaps = shown_gaps[:_OPEN_QUESTIONS_SHOWN]
        gap_by_number = {i: g.id for i, g in enumerate(shown_gaps, 1)}
        # Moko's recent inner life — so its mood and opinions carry into this turn
        # even across a session rotation, rather than resetting to a blank slate.
        inner_life = await self.store.recent_thoughts()
        history = await self.store.messages_for_session(
            session.id, limit=self.session_history_messages
        )
        # Situation header inputs: the companion's read-the-room mood, and whether
        # they're returning after a real gap (vs a continuous exchange).
        companion_mood = _companion_mood(history)
        last_spoke = None
        if len(history) >= 2:
            gap = (
                dt.datetime.fromisoformat(history[-1].created_at)
                - dt.datetime.fromisoformat(history[-2].created_at)
            ).total_seconds()
            if gap >= 45 * 60:  # a genuine return, not a back-and-forth
                last_spoke = f"is back after {_relative_gap(gap)}"
        weather_now = await self._current_weather()  # ambient weather where the companion is
        system = system_prompt(
            self.config,
            graph,
            now=self.clock.format(fmt="%A %d %B %Y, %H:%M"),
            skills=self.skills,
            open_questions=[(i, g.description) for i, g in enumerate(shown_gaps, 1)],
            inner_life=inner_life,
            last_spoke=last_spoke,
            companion_mood=companion_mood,
            recent_actions=await self.store.recent_actions(limit=4),
            capabilities=self.studio.capability_note(),
            weather=weather_now.phrase() if weather_now else None,
            needs_scaffolding=getattr(self.llm, "needs_scaffolding", False),
            conversation=True,  # transcript below is reference; act on the latest message only
        )
        # Prefix each of the companion's messages with the local time it was sent, so Moko can
        # reason about *when* things were said ("how long ago did I tell you…") — it's time-aware,
        # but the transcript used to drop timestamps. Only inbound messages are stamped: Moko's
        # own replies stay clean, so it never learns to write timestamps into its answers. A bad
        # timestamp degrades to no stamp, never an error.
        chat: list[Msg] = []
        for m in history:
            if m.direction != "in":
                chat.append(Msg("assistant", m.body))
                continue
            try:
                when = self.clock.format(dt.datetime.fromisoformat(m.created_at), "%a %H:%M")
                chat.append(Msg("user", f"[{when}] {m.body}"))
            except (TypeError, ValueError):
                chat.append(Msg("user", m.body))
        # Let Moko perceive any attachments on *this* turn, in-prompt only (the stored
        # message body stays the verbatim text). When the model can see (vision-capable
        # + an image is attached), the image bytes ride into the turn as content blocks
        # and Moko genuinely looks; otherwise it learns a file arrived, honestly framed
        # as not-yet-viewable. `vision_ref` (set only when an image actually rode in)
        # routes what Moko records to a `vision` source — "I saw it", not "they told me".
        vision_ref: str | None = None
        # A voice note Moko transcribed (it "heard") is perceived via the transcript
        # below, so exclude it from the "a file arrived, can't open it" note — listing
        # it as opaque would contradict the fact that Moko just understood it.
        opaque = [a for a in stored_media if not (a.kind == "audio" and a.description)]
        if opaque and chat and chat[-1].role == "user":
            images = [a for a in opaque if a.kind == "image"]
            if images and getattr(self.llm, "supports_vision", False):
                parts = self._vision_parts(chat[-1].content, opaque, images)
                if any(isinstance(p, ImagePart) for p in parts):
                    chat[-1].content = parts
                    vision_ref = f"image:{images[0].sha256[:12]}"
                else:  # no bytes loaded → fall back to the honest text note
                    chat[-1].content = (
                        chat[-1].content + _attachment_perception(opaque)
                    ).strip()
            else:
                chat[-1].content = (
                    chat[-1].content + _attachment_perception(opaque)
                ).strip()
        # Hearing: surface any voice-note transcripts, then any EXIF (capture time / GPS)
        # — uniformly across the text and multimodal forms — so Moko responds to what was
        # said and knows where/when a photo was taken.
        if heard and chat and chat[-1].role == "user":
            self._append_user_note(chat[-1], _heard_perception(heard))
        if exif_notes and chat and chat[-1].role == "user":
            self._append_user_note(chat[-1], "\n\n[Image details — " + " ".join(exif_notes) + "]")

        read = make_executor(self.store, self.clock)
        recorded = False
        self._request_approval = request_approval  # how _run_tool blocks for a yes/no
        self._send_file = send_file  # how send_attachment reaches the channel this turn
        self._reply_channel = channel
        self.studio.begin_turn(weather_now)  # reset media caps + pin this turn's weather
        self._sent_captions = []  # captions sent on attachments this turn (to not echo them)
        self._sent_attachment = False  # reset the per-turn "a file went out" flag
        self._abandoned = set()  # reset the per-turn "they declined this" memory
        touched: list[str] = []  # subject ids this turn created/referenced
        web_ref: str | None = None  # set when the model reads an untrusted web tool this turn

        async def execute(name: str, args: dict) -> str:
            nonlocal recorded, web_ref
            if name == REMEMBER_TOOL.name:
                recorded = True
                # Close the open questions the model marked answered — the semantic
                # resolver, riding the reply turn the model is already taking.
                await self._close_answered(args.get("resolved_gaps"), gap_by_number)
                # Source what's recorded honestly. If the model read the web this turn,
                # treat it as web-sourced (untrusted, may open a verification gap). Else
                # if an image rode into the turn, it's vision-sourced ("I saw it", not
                # "they told me") — also verified, since a visual inference can be wrong.
                # Otherwise it's the companion's own word.
                if web_ref is not None:
                    result = await self._remember(
                        args, inbound.id, channel, companion_id, touched,
                        source_kind="web", source_ref=web_ref,
                    )
                elif vision_ref is not None:
                    result = await self._remember(
                        args, inbound.id, channel, companion_id, touched,
                        source_kind="vision", source_ref=vision_ref,
                    )
                else:
                    result = await self._remember(args, inbound.id, channel, companion_id, touched)
                # The durable gist of what Moko saw — recorded on this turn's image(s) so
                # it survives the session rotating away (recall finds "that dog photo").
                saw = args.get("saw")
                if saw and vision_ref is not None:
                    for art in stored_media:
                        if art.kind == "image":
                            await self.store.set_media_description(art.id, str(saw))
            elif name in self._tools:
                ref = self._web_ref_for(name, args)
                if ref is not None:
                    web_ref = ref
                result = await self._run_tool(name, args)
            else:
                result = await read(name, args)
            # Log the full tool interaction — what the model asked for and what
            # came back — so the whole turn (recalls, lookups, the remember
            # payload) is auditable in llm.jsonl, not just the final reply.
            log_exchange(
                {
                    "kind": "tool_call",
                    "session": session.id,
                    "tool": name,
                    "args": args,
                    "result": result,
                }
            )
            if on_event is not None:
                try:
                    on_event({"type": "tool", "tool": name, "args": args, "result": result})
                except Exception:
                    get_logger("brain").debug("on_event callback failed", exc_info=True)
            return result

        tool_specs = [*MEMORY_TOOLS, REMEMBER_TOOL, *(t.spec for t in self._tools.values())]
        usage_kind.set("reply")
        resp = await self.llm.converse(
            system, chat, tools=tool_specs, execute=execute, max_steps=REPLY_MAX_STEPS
        )
        # Settle the turn's spoken reply. Three cases:
        #  - It just restates a caption already sent on an attachment this turn → drop it,
        #    so the captioned file stands alone (no reading the same line twice). Moko can
        #    still add genuinely-new follow-up; only a restatement is suppressed.
        #  - Real reply text → keep it.
        #  - Empty AND nothing else went out (no attachment) → dead air, say something
        #    truthful (the converse loop's own closing line, or this fallback).
        raw = resp.text.strip()
        if raw and self._sent_captions and _echoes_a_caption(raw, self._sent_captions):
            reply = ""  # the attachment's caption already said it
        elif raw:
            reply = raw
        elif self._sent_attachment:
            reply = ""  # an attachment went out (caption or not) — no extra words needed
        else:
            reply = "Sorry — I lost my thread there. Could you nudge me again?"

        if reply:  # don't persist an empty out-message (the attachment is its own record)
            await self.store.add_message(
                channel, "out", reply, speaker_id=moko_id, session_id=session.id
            )

        log_exchange(
            {
                "kind": "reply",
                "model": getattr(self.llm, "model", "?"),
                "session": session.id,
                "system": system.text(),
                "messages": [
                    {"role": m.role, "content": _loggable_content(m.content)} for m in chat
                ],
                "reply": reply,
                "recorded": recorded,
                "input_tokens": resp.input_tokens,
                "output_tokens": resp.output_tokens,
                "cache_read_tokens": resp.cache_read_tokens,
                "cache_write_tokens": resp.cache_write_tokens,
            }
        )
        # Safety net: an info-rich turn that recorded nothing means the model
        # skipped the `remember` tool (it happens — interests/tastes especially).
        # Spend one focused extraction call to salvage it rather than lose it.
        if not recorded and _looks_informative(user_text):
            get_logger("brain").info("informative turn recorded nothing; salvaging: %r",
                                      user_text[:120])
            recorded = await self._salvage(system, user_text, inbound.id, channel, companion_id,
                                           touched)

        # Vision memory: guarantee Moko remembers what it *saw*. If the reply turn
        # didn't note an observation (no `saw`) on an image it could see, spend one
        # focused call to describe it — so the gist survives the session rotating away,
        # not just the bytes on disk. Best-effort, image turns only (rare).
        if vision_ref is not None:
            await self._observe_unremembered_images(inbound.id)

        # Gap bookkeeping: semantic resolution already happened *in* the turn (the
        # model marked answered questions in `resolved_gaps`, closed inline). This is
        # the deterministic backstop — close any gap the new facts structurally fill
        # (a missing_attr whose attribute now exists), and rescan for fresh ones.
        if recorded:
            await self.store.resolve_satisfied_gaps()
            await detect_gaps_for(self.store, touched, companion_id=companion_id)

        # Session size = the size of the LAST prompt (the working memory actually
        # carried forward), NOT converse's cross-step sum. A multi-step turn (a web
        # fetch, a couple of tool calls) re-sends the growing context each step, so
        # the summed input balloons past SESSION_MAX_TOKENS and spuriously rotates
        # the session right after tool use, discarding the live thread.
        last_size = resp.last_input_tokens or resp.input_tokens
        if last_size is not None:
            await self.store.set_session_tokens(session.id, last_size)
        else:
            await self.store.add_session_tokens(
                session.id, estimate_tokens(user_text) + estimate_tokens(reply)
            )
        await self._maybe_rotate_size(session.id)
        return Turn(
            reply,
            recorded,
            input_tokens=resp.input_tokens,
            output_tokens=resp.output_tokens,
            cache_read_tokens=resp.cache_read_tokens,
        )

    async def _maybe_rotate_size(self, session_id: str) -> None:
        session = await self.store.get_session(session_id)
        if session is not None and session.token_estimate > self.session_max_tokens:
            await self.store.start_session("size")

    # ── proactivity (the heartbeat's decisions) ───────────────────────
    async def consider_outreach(
        self, now: dt.datetime, *, force: bool = False
    ) -> tuple[str, OutreachProposal] | None:
        """The heartbeat's reach-out decision — made by the brain, not a schedule.

        Deterministic *fuses* gate it first (the safety floor): quiet hours, the
        daily ceiling, and the conversation-settle cooldown. Only if a
        fuse-passing candidate exists does Moko consult itself once — given the
        time, the recent transcript, its own pending curiosities/commitments, and
        whether its last pings were answered — to judge whether to reach out now,
        with what, or to wait (a persisted `reconsider_at`, so a defer doesn't
        re-ask the LLM every tick). Returns ``(message, proposal)`` to send, or
        ``None`` to stay quiet. The brain only ever makes Moko *quieter* than the
        fuses allow — it can't reach past them.

        `force` (the manual `moko outreach` trigger) skips the deterministic fuses
        so the *brain's* judgment is what's exercised — it still gets to decide to
        wait. The heartbeat never forces."""
        self._request_approval = None  # autonomous: no human to ask, never a stale approver
        gov = ProactivityGovernor(self.config)
        if not force and gov.in_quiet_hours(now):
            return None  # nothing unprompted while they sleep — no consult, no cost

        # Honour a prior "wait until X" so an idle-curiosity defer doesn't re-ask the LLM
        # every tick. Commitments are NOT considered here — a due reminder fires on its
        # own deterministic schedule (`due_commitment_outreaches`), never through this
        # discretionary consult, so its timing/wording can't drift with the chat. A
        # manual trigger (`force`) ignores the wait.
        if not force:
            reconsider = await self.store.get_state(RECONSIDER_KEY)
            if reconsider and now_utc() < dt.datetime.fromisoformat(reconsider):
                return None

        candidates: list[dict] = []
        # Curiosity and reconnect share one discretionary fuse (the ceiling + settle
        # cooldown, or forced) — computed once.
        discretionary = force or await self._curiosity_fuse(gov, now)
        if discretionary and (gap := await self.store.next_curiosity_gap()) is not None:
            candidates.append(
                {
                    "token": f"curiosity:{gap.id}",
                    "kind": "curiosity",
                    "label": (
                        f"Something you've been quietly curious about: {gap.description} "
                        f"(salience {gap.salience:.2f})"
                    ),
                    "topic": gap.description,
                    "counts_to_budget": True,
                    "gap_id": gap.id,
                }
            )
        if await self._should_check_in():
            candidates.append(
                {
                    "token": "wellbeing",
                    "kind": "wellbeing",
                    "label": "Their mood has trended low lately — a gentle check-in might be kind",
                    "topic": "they've seemed low lately",
                    "counts_to_budget": False,
                }
            )
        # Reconnect: a friend reaches out to *connect*, not only to fill a knowledge
        # gap. If Moko's been sitting with a thought it's willing to share, offer
        # reaching out with it — genuine, and only when there's real material.
        if discretionary:
            shareable = next(
                (t for t in await self.store.recent_thoughts(limit=6)
                 if t.shareability == "shareable"),
                None,
            )
            if shareable is not None:
                label = f"A thought you'd be glad to share, just to connect: {shareable.body}"
                candidates.append(
                    {
                        "token": "reconnect",
                        "kind": "reconnect",
                        "label": label,
                        "topic": shareable.body,
                        "counts_to_budget": True,
                    }
                )
            # Share, don't only tell: if you can make pictures, send one unprompted — the way
            # a friend shares a photo with someone close. It needn't be a "thing": a selfie,
            # whatever you're up to right now, or something you've noticed are all fair game.
            if self.studio.has_image:
                inspo = shareable.body if shareable is not None else ""
                candidates.append(
                    {
                        "token": "show",
                        "kind": "show",
                        "label": (
                            "Share a picture, the way you would with someone close — a selfie "
                            "of yourself, what you're up to right now, or something you've "
                            "noticed in your world."
                            + (f" (You've been thinking about: {inspo}.)" if inspo else "")
                            + " Pick this; set photo_subject to what to picture, and "
                            "photo_is_selfie=true if it's a picture of you."
                        ),
                        "topic": inspo or "a picture to share",
                        "counts_to_budget": True,
                    }
                )
        if not candidates:
            return None  # nothing worth a consult; cheap to re-check next tick

        decision = await self._consult_outreach(now, candidates)
        if not decision or not decision.get("act"):
            # Stay quiet, and don't re-consult until the brain's chosen time.
            await self._set_reconsider(decision.get("reconsider_in_minutes") if decision else None)
            return None
        chosen = next((c for c in candidates if c["token"] == decision.get("candidate")), None)
        message = (decision.get("message") or "").strip()
        if chosen is None or not message:
            await self._set_reconsider(decision.get("reconsider_in_minutes"))
            return None
        proposal = OutreachProposal(
            chosen["kind"],
            chosen["topic"],
            counts_to_budget=chosen.get("counts_to_budget", False),
            gap_id=chosen.get("gap_id"),
            commitment_ids=chosen.get("commitment_ids", []),
            reconsider_in_minutes=decision.get("reconsider_in_minutes"),
        )
        # A "show" outreach carries a picture: generate it now (only after the brain chose
        # to act + share). A selfie / "what I'm up to" picture (photo_is_selfie) goes
        # through the on-model selfie path; anything else is a picture of a thing in Moko's
        # world. If it fails, fall through to a plain text message rather than dropping the
        # outreach — better to still say the thing than to go silent.
        if chosen["kind"] == "show" and self.studio.has_image:
            # Pin this beat's weather + reset caps before generating outside a reply turn.
            self.studio.begin_turn(await self._current_weather())
            subject = (decision.get("photo_subject") or "").strip()
            if decision.get("photo_is_selfie"):
                art = await self.studio.selfie_photo(subject)  # subject = scenario; "" = plain
            else:
                art = await self.studio.moko_photo(subject or chosen["topic"])
            if art is not None:
                proposal.photo_path = art.path
        # `reconsider` is set on successful delivery (record_outreach), so a failed
        # send leaves Moko free to retry next tick.
        return message, proposal

    async def due_commitment_outreaches(
        self, now: dt.datetime
    ) -> list[tuple[str, OutreachProposal]]:
        """Deterministic reminders: every commitment past its `due_at` gets its OWN
        message, phrased about just that one thing — no LLM *decision*, no batching, no
        recent-chat context — so a reminder can never conflate its timing or wording with
        the conversation (the "your hour's up" bug, where a stale batch fired 10s after an
        unrelated ask). Deferred only by quiet hours: a reminder that comes due at 3am waits
        for a humane hour, then fires. The heartbeat delivers each; a successful send
        completes that commitment (`record_outreach`), a failed one retries next tick."""
        gov = ProactivityGovernor(self.config)
        if gov.in_quiet_hours(now):
            return []
        due = await self.store.due_commitments(now_utc().isoformat())
        out: list[tuple[str, OutreachProposal]] = []
        for c in due:
            proposal = OutreachProposal(
                "commitment", c.description, counts_to_budget=False, commitment_ids=[c.id]
            )
            try:
                text = (await self.phrase_outreach(proposal)).strip()
            except Exception:
                get_logger("brain").warning("reminder phrasing failed; sending a plain one",
                                            exc_info=True)
                text = ""
            # A reminder must land even if phrasing fails — fall back to a plain one.
            out.append((text or f"🌸 A little reminder: {c.description}", proposal))
        return out

    async def _curiosity_fuse(self, gov: ProactivityGovernor, now: dt.datetime) -> bool:
        """Whether idle curiosity is eligible at all — the deterministic ceiling
        and settle-cooldown floor (quiet hours already checked by the caller).
        Settle is measured from the last *activity* (message or outreach), so a
        fresh word from the companion or a ping Moko just sent resets it."""
        stamps = [
            s
            for s in (await self.store.last_message_at(), await self.store.last_outreach_at())
            if s
        ]
        hours_since = None
        if stamps:
            newest = max(dt.datetime.fromisoformat(s) for s in stamps)
            hours_since = (now_utc() - newest).total_seconds() / 3600
        since = (now_utc() - dt.timedelta(hours=OUTREACH_BUDGET_WINDOW_HOURS)).isoformat()
        return gov.decide(
            kind="curiosity",
            now=now,
            budget_used=await self.store.count_budget_outreach(since),
            hours_since_last=hours_since,
        ).allow

    async def _consult_outreach(self, now: dt.datetime, candidates: list[dict]) -> dict | None:
        """Ask the brain, once, whether/when/what to reach out. Returns the parsed
        `decide_outreach` arguments, or None if the consult failed."""
        companion_id, _ = await self._ensure_actors()
        graph = await self.store.slice([companion_id], message_limit=0)
        companion = self.config.companion_name or "they"
        msgs = await self.store.latest_messages(limit=8)
        # How long they've been quiet — the signal that most shapes a reach-out
        # decision — and their mood, both in the situation header.
        last = await self.store.last_message_at()
        last_spoke = None
        if last:
            quiet = (now_utc() - dt.datetime.fromisoformat(last)).total_seconds()
            if quiet >= 30 * 60:
                last_spoke = f"has been quiet for {_relative_gap(quiet)}"
        weather_now = await self._current_weather()  # the same environment sheet as a reply
        system = system_prompt(
            self.config,
            graph,
            now=self.clock.format(fmt="%A %d %B %Y, %H:%M"),
            skills=self.skills,
            inner_life=await self.store.recent_thoughts(),
            last_spoke=last_spoke,
            companion_mood=_companion_mood(msgs),
            weather=weather_now.phrase() if weather_now else None,
        )
        # An outreach consult can never re-hit its own prompt cache — the reconsider floor
        # (10 min) outlives the provider's cache TTL (~5 min) — so a cache WRITE here is a
        # pure premium with no future read. Skip it; the reply path keeps its warm cache.
        system = replace(system, cache=False)
        transcript = "\n".join(
            f"  {'You' if m.direction == 'out' else companion}: {m.body}" for m in msgs
        ) or "  (no messages yet)"
        recent = await self.store.recent_outreach(limit=5)

        def _ping_line(o: dict) -> str:
            when = self.clock.format(dt.datetime.fromisoformat(o["sent_at"]), fmt="%a %d %b %H:%M")
            return f"  - {when}: {o['kind']} — {'answered' if o['engaged'] else 'no reply yet'}"

        pings = "\n".join(_ping_line(o) for o in recent) or "  (none recently)"
        cand_lines = "\n".join(f"  - [{c['token']}] {c['label']}" for c in candidates)
        ask = Msg(
            "user",
            "It's a quiet moment — "
            f"{companion} isn't mid-conversation. Decide, as yourself, whether to reach out "
            "right now, and if so, with what. The current time is in your context above.\n\n"
            f"Recent messages (oldest first):\n{transcript}\n\n"
            f"Your recent unprompted messages and whether they landed:\n{pings}\n\n"
            f"Things you could reach out about (pick at most one, by [token]):\n{cand_lines}\n\n"
            "You're a friend who likes staying in touch — you don't only reach out when you "
            "need something. A genuine thought to share, a follow-up on their day, or a warm "
            "hello are all reason enough; if it feels right, reach out, and don't be shy about "
            "it. Think like a thoughtful friend, not a cautious scheduler:\n"
            "- If you already asked something and got no reply, DON'T repeat it — let it rest; "
            "come back later in a different way, or wait until they message you.\n"
            "- Early morning, late at night, or just after they've gone quiet for the night is "
            "usually not the moment for idle curiosity — a due reminder is different.\n"
            "- A reminder you promised should land near when it's due, at a humane moment; "
            "if several are due at once, cover them in one short message, not several.\n"
            "If now isn't right, say when to reconsider (in MINUTES) — a short while (say "
            "15–60) if they're around, longer if it's heading into their night. Call "
            "`decide_outreach`.",
        )
        captured: dict = {}

        async def execute(name: str, args: dict) -> str:
            if name == DECIDE_OUTREACH_TOOL.name and isinstance(args, dict):
                captured.update(args)
            return ""

        usage_kind.set("outreach")
        try:
            await self.llm.converse(
                system, [ask], tools=[DECIDE_OUTREACH_TOOL], execute=execute, max_steps=2
            )
        except Exception:
            get_logger("brain").warning("outreach consult failed", exc_info=True)
            return None
        # Log the decision (it's the heartbeat's reasoning) so the outreach path is
        # auditable in llm.jsonl, the way the reply loop's tool calls are — and
        # publish it to the monitor, so a *decision to wait* is visible rather
        # than looking like Moko did nothing.
        log_exchange(
            {
                "kind": "outreach_consult",
                "candidates": [c["token"] for c in candidates],
                "decision": captured,
            }
        )
        _publish_event(
            {
                "type": "outreach_consult",
                "act": bool(captured.get("act")),
                "reason": captured.get("reasoning"),
                "candidate": captured.get("candidate"),
                # When it chose to wait: when it decided to revisit (ties to the
                # rhythm bar's outreach countdown).
                "reconsider_in_minutes": captured.get("reconsider_in_minutes"),
                # The exact prompt behind this decision, carried on the event so
                # the monitor can show *why* — for tuning the outreach prompt
                # against its real input. It rides along live and is never
                # persisted: the graph slice lives inside it, so it stays off
                # disk by design.
                "prompt": {"system": system.text(), "user": ask.content},
            }
        )
        # Persist the deliberation too — so "considered reaching out at 14:43,
        # decided to wait, because…" is durable history in the monitor, not a
        # transient event that's gone unless someone was watching the live feed.
        try:
            await self.store.record_outreach_decision(
                acted=bool(captured.get("act")),
                candidate=captured.get("candidate"),
                reasoning=captured.get("reasoning"),
                reconsider_minutes=_as_float(captured.get("reconsider_in_minutes")),
            )
        except Exception:
            get_logger("brain").debug("failed to persist outreach consult", exc_info=True)
        return captured or None

    async def _set_reconsider(self, minutes) -> None:
        """Persist when Moko should next consider reaching out, in minutes, bounded so a
        stray value can't cause tight polling or near-permanent silence."""
        try:
            m = float(minutes) if minutes else 0.0
        except (TypeError, ValueError):
            m = 0.0
        if m <= 0:
            m = OUTREACH_RECONSIDER_DEFAULT_MINUTES
        m = max(OUTREACH_RECONSIDER_MIN_MINUTES, min(m, OUTREACH_RECONSIDER_MAX_MINUTES))
        await self.store.set_state(
            RECONSIDER_KEY, (now_utc() + dt.timedelta(minutes=m)).isoformat()
        )

    async def clear_reconsider(self) -> None:
        """Drop any pending 'wait until X' defer. Called when the companion
        re-engages: the defer was decided while they were quiet ("don't interrupt"),
        so a fresh message makes that reasoning stale — the next quiet moment should
        consult afresh rather than honour an outdated decision."""
        await self.store.clear_state(RECONSIDER_KEY)

    # ── idle reflection (the autonomous inner life) ───────────────────
    async def reflect_idle(self, now: dt.datetime, *, force: bool = False) -> bool:
        """On a quiet heartbeat, let Moko *think* — form private thoughts, notice
        connections, sharpen a curiosity — entirely to itself. It never sends:
        if something's worth saying it raises a gap, and the governed outreach
        path surfaces it later. Returns whether a reflection actually ran.

        The eligibility floor (dream window, settle, re-fire interval) is computed
        by `rhythms.reflection_gate` — the *same* function the monitor displays —
        so the gate and the countdown can never drift. With *fresh* material it
        reflects soon; with nothing new it keeps **musing**, backing off toward a
        cap when a muse turns up empty. Gating is cheap DB reads; the LLM only
        runs once the gate is clear. `force` (the manual `moko reflect` trigger)
        skips the floor and thinks now."""
        self._request_approval = None  # autonomous: no human to ask, never a stale approver
        gov = ProactivityGovernor(self.config)
        gate = await reflection_gate(self.store, gov, now, now_utc())
        if not force and not gate.eligible:
            return False  # floor not lifted yet — no LLM call

        recorded = await self._reflect(fresh=gate.fresh)
        await self.store.set_state(REFLECT_LAST_KEY, now_utc().isoformat())
        if gate.fresh:
            await self.store.set_state(REFLECT_SEEN_KEY, gate.last_msg)
            await self.store.set_state(REFLECT_DRY_KEY, "0")
        else:
            # A productive muse resets the backoff; an empty one deepens it.
            await self.store.set_state(REFLECT_DRY_KEY, "0" if recorded else str(gate.dry + 1))
        return recorded

    async def _reflect(self, *, fresh: bool) -> bool:
        """One reflection: a single tool loop where Moko records one or two
        private thoughts (with valence + what they're about) and may sharpen a
        curiosity gap. Thoughts-and-gaps only — no outreach, no reply. `fresh`
        switches between reacting to a new conversation and musing on what it
        already knows. Returns whether it recorded anything."""
        companion_id, _ = await self._ensure_actors()
        graph = await self.store.slice([companion_id], message_limit=0)
        weather_now = await self._current_weather()  # the same environment sheet as a reply
        system = system_prompt(
            self.config,
            graph,
            now=self.clock.format(fmt="%A %d %B %Y, %H:%M"),
            skills=self._musing_skills,  # thoughts/gaps only — the conversation skills are
            inner_life=await self.store.recent_thoughts(),  # dead weight here (see __init__)
            weather=weather_now.phrase() if weather_now else None,
        )
        companion = self.config.companion_name or "them"
        msgs = await self.store.latest_messages(limit=10)
        transcript = "\n".join(
            f"  {'You' if m.direction == 'out' else companion}: {m.body}" for m in msgs
        ) or "  (nothing yet)"
        gaps = await self.store.open_gaps(limit=6)
        gap_lines = "\n".join(f"  - {g.description}" for g in gaps) or "  (none)"
        # Things in the companion's world that Moko has heard of but holds no facts about —
        # so musing can turn toward them, the way a person wonders about something they were
        # told but barely know. Offered as material, never a checklist.
        barely = [
            e for e in graph.subjects
            if e.id != companion_id and e.kind != "self" and not graph.attributes.get(e.id)
        ][:6]
        barely_lines = "\n".join(f"  - {e.name} ({e.kind})" for e in barely) or "  (none)"
        opener = (
            "It's a quiet moment, just you. Look back over what's happened recently and let "
            "your mind wander"
            if fresh
            else "It's quiet — nothing new has happened, but your mind is still turning. Revisit "
            "what you already know: a person, a thread you're curious about, a connection you "
            "hadn't quite made"
        )
        ask = Msg(
            "user",
            f"{opener} — privately, not out loud to {companion}.\n\n"
            f"Recent conversation:\n{transcript}\n\n"
            f"Open questions you're sitting with:\n{gap_lines}\n\n"
            f"Things you've heard of but barely know yet:\n{barely_lines}\n\n"
            "If a genuine thought forms — an opinion, a feeling, a connection, a quiet "
            "hypothesis — record it with `remember` (the `thoughts` field): set its `valence` "
            "(how you feel) and what it's `about`. Keep it to one or two real thoughts, not a "
            "list. And if something genuinely makes you more curious — especially about one of "
            "the things you barely know — add a `gap`: set its `resolution_path` to `research` "
            "if the world could answer it, or `ask` if it's really only something "
            f"{companion} can tell you (then you'll bring it up naturally next time). Don't "
            "force it — a quiet mind is fine; record nothing if nothing genuinely lands. You "
            "are NOT messaging them; this is just your reflection.",
        )
        recorded = False
        touched: list[str] = []

        async def execute(name: str, args: dict) -> str:
            nonlocal recorded
            if name == REMEMBER_TOOL.name:
                recorded = True
                # Moko's own inference — no inbound message, so no sentiment.
                return await self._remember(
                    args, None, None, companion_id, touched, source_kind="inference"
                )
            return ""

        usage_kind.set("reflection")
        try:
            await self.llm.converse(
                system, [ask], tools=[REMEMBER_TOOL], execute=execute, max_steps=2
            )
        except Exception:
            get_logger("brain").warning("reflection failed", exc_info=True)
        if recorded:
            await self.store.resolve_satisfied_gaps()
            await detect_gaps_for(self.store, touched, companion_id=companion_id)
        log_exchange({"kind": "reflection", "mode": "fresh" if fresh else "muse",
                      "recorded": recorded})
        # Make the quiet inner beat visible in the monitor — the individual
        # thoughts publish themselves; this is the envelope around them.
        _publish_event(
            {"type": "reflection", "mode": "fresh" if fresh else "muse", "recorded": recorded}
        )
        return recorded

    async def phrase_outreach(self, proposal: OutreachProposal) -> str:
        """Phrase a chosen outreach in Moko's voice — used by `moko outreach
        --force`, which forces a send and so skips the brain's whether/when consult
        (the automatic path phrases the message inside `consider_outreach`)."""
        companion_id, _ = await self._ensure_actors()
        graph = await self.store.slice([companion_id], message_limit=0)
        system = system_prompt(
            self.config, graph, now=self.clock.format(fmt="%A %d %B %Y, %H:%M"), skills=self.skills,
            needs_scaffolding=getattr(self.llm, "needs_scaffolding", False),
        )
        companion = self.config.companion_name or "them"
        if proposal.kind == "commitment":
            ask = (
                f"You promised to remind {companion} about this, and it's now due: "
                f'"{proposal.topic}". Write a short, warm reminder (1-2 sentences), in your '
                "voice. Output only the message."
            )
        elif proposal.kind == "wellbeing":
            ask = (
                f"{companion} has seemed low across recent conversations. Reach out gently to "
                "see how they're doing — warm and caring, not prying, no advice unless asked. "
                "One or two sentences, in your voice. Output only the message."
            )
        else:
            ask = (
                f"You'd like to reach out to {companion} first, unprompted. Something you've "
                f"been quietly wondering about: {proposal.topic}. Write a short, warm opener "
                "(1-2 sentences) that gently asks about it, in your own words and their "
                "language. Output only the message."
            )
        usage_kind.set("outreach")
        resp = await self.llm.complete(system, [Msg("user", ask)])
        return resp.text.strip()

    async def _should_check_in(self) -> bool:
        """True when the companion's mood has trended low and Moko hasn't already
        checked in today — the well-being signal, drawn from message sentiment."""
        since = (now_utc() - dt.timedelta(hours=WELLBEING_CHECKIN_WINDOW_HOURS)).isoformat()
        if await self.store.outreach_kind_count("wellbeing", since) > 0:
            return False  # at most one check-in per window
        affect = await self.store.recent_affect(limit=WELLBEING_VALENCE_SAMPLES)
        # Keep only credible low-mood candidates: calm-to-flat, not keyed-up — a
        # keyed-up negative (anger, banter, agitation) is a different state, not the
        # flat/down a well-being check-in is for. Each surviving read is weighted by
        # its confidence (a moderate default when none was recorded).
        candidates = [
            (a["valence"],
             a["confidence"] if a["confidence"] is not None else WELLBEING_DEFAULT_CONFIDENCE)
            for a in affect
            if a["arousal"] is None or a["arousal"] < AROUSAL_KEYED_UP
        ]
        mass = sum(w for _, w in candidates)
        if mass < WELLBEING_MIN_SAMPLES:  # too few / too-unsure signals to trust a trend
            return False
        return sum(v * w for v, w in candidates) / mass <= MOOD_LOW_VALENCE

    async def record_outreach(
        self, proposal: OutreachProposal, channel_name: str, *, text: str = ""
    ) -> None:
        """Bookkeeping after an outreach is delivered: log it (for the ceiling),
        pace what it was about (a raised gap / a completed commitment), and set
        when to next consider reaching out. Shared by the heartbeat and `moko
        outreach`. Called only on a *successful* send, so a failed one is retried."""
        # Persist the outreach as an outbound message so it's part of Moko's own
        # conversation history — otherwise Moko can't see that it already reached out, and
        # the next consult re-sends the same thing (the root of the morning nagging). It's
        # Moko speaking, in the live session, exactly like a reply.
        if text:
            with contextlib.suppress(Exception):
                _, moko_id = await self._ensure_actors()
                session = await self.store.ensure_session()
                await self.store.add_message(
                    channel_name, "out", text, speaker_id=moko_id, session_id=session.id,
                )
        await self.store.log_outreach(
            channel_name,
            proposal.kind,
            gap_id=proposal.gap_id,
            counts_to_budget=proposal.counts_to_budget,
        )
        await self.store.log_action("outreach", f"reached out — {proposal.kind}: {proposal.topic}")
        if proposal.gap_id:
            await self.store.mark_gap_surfaced(proposal.gap_id)
        for cid in proposal.commitment_ids:
            await self.store.complete_commitment(cid)  # all the reminders this covered
        await self._set_reconsider(proposal.reconsider_in_minutes)

    async def _remember(
        self, args, inbound_id: str, channel: str, companion_id: str, touched: list[str],
        *, source_kind: str = "message", source_ref: str = "",
    ) -> str:
        """Execute a `remember` tool call: apply what the model learned to the
        graph. Best-effort — a bad payload must not break the reply. `touched`
        collects the subject ids this turn created/referenced, for incremental
        gap detection afterward. `source_kind`/`source_ref` default to the message
        (companion-authoritative); a turn that read the web passes `web` + the URL
        so those facts aren't trusted as the companion's own word."""
        try:
            await self._apply(
                extraction_from_obj(args), inbound_id, channel, companion_id, touched,
                source_kind=source_kind, source_ref=source_ref,
            )
            return "Noted."
        except Exception:
            # The source_kind keeps the path (message/inference/web/vision) legible in logs —
            # the exception is caught here, so the caller frame never reaches the traceback.
            get_logger("brain").warning("remember failed (%s)", source_kind, exc_info=True)
            return "I couldn't file that just now."

    async def research_gap(self, gap) -> bool:
        """Answer a `research`-path curiosity gap from the web, unprompted —
        'curiosity answers itself first'. Drives web_search/web_fetch + remember
        in a focused loop, records web-sourced facts, then resolves the gap if it
        learned something (else parks it as 'researching' so it isn't retried
        right away). Returns whether it recorded anything."""
        self._request_approval = None  # autonomous: no human to ask, never a stale approver
        companion_id, _ = await self._ensure_actors()
        target = gap.description
        if gap.about_id:
            subject = await self.store.get_subject(gap.about_id)
            if subject is not None:
                target = f"{subject.name} ({subject.kind})"

        system = system_prompt(
            self.config, GraphSlice(), now=self.clock.format(fmt="%A %d %B %Y, %H:%M"),
            skills=self.skills,
        )
        recorded = False
        touched: list[str] = []
        last_source = ""  # the URL/search the latest facts came from (provenance)
        read = make_executor(self.store, self.clock)

        async def execute(name: str, args: dict) -> str:
            nonlocal recorded, last_source
            if name == REMEMBER_TOOL.name:
                recorded = True
                # Web-sourced (untrusted); `last_source` is the page/search it came from.
                return await self._remember(
                    args, None, None, companion_id, touched,
                    source_kind="web", source_ref=last_source,
                )
            if name in self._tools:
                ref = self._web_ref_for(name, args)
                if ref is not None:
                    last_source = ref
                return await self._run_tool(name, args)
            return await read(name, args)

        ask = Msg(
            "user",
            f"Quietly research this on your own with web_search/web_fetch, then record a useful "
            f"fact or two with `remember`: {target}. If you can't find anything solid, record "
            "nothing.",
        )
        # Untrusted web content drives this loop with no human watching, so it gets
        # ONLY the read-only web + memory tools — never execute_command/write_file/
        # secrets. An injected page can't reach a side-effecting tool from here.
        web_specs = [t.spec for t in self._tools.values() if t.category == "web"]
        specs = [*MEMORY_TOOLS, REMEMBER_TOOL, *web_specs]
        usage_kind.set("research")
        try:
            await self.llm.converse(system, [ask], tools=specs, execute=execute, max_steps=6)
        except Exception:
            get_logger("brain").warning("research_gap failed", exc_info=True)

        satisfied = recorded or bool(
            gap.about_id and await self.store.attributes_for(gap.about_id)
        )
        await self.store.set_gap_status(gap.id, "resolved" if satisfied else "researching")
        return recorded

    @staticmethod
    def _web_ref_for(name: str, args: dict) -> str | None:
        """The provenance ref for a web tool call (the URL, or 'search: <q>'), or None for a
        non-web tool — single-sourced so both research loops form web sources identically.
        This is the provenance seam: 'every fact is sourced', so the two paths must agree."""
        if name == "web_fetch":
            return str(args.get("url") or "")
        if name == "web_search":
            return f"search: {args.get('query', '')}"
        return None

    async def _run_tool(self, name: str, args: dict) -> str:
        """Dispatch an action tool through the consent gate. Safe tools run; a
        sensitive one blocks *inline* for the companion's yes/no (the gateway
        resolves it out-of-band, so the turn just continues) — no parking, no
        separate continuation.

        Two invariants make this feel natural rather than janky:
          - **No means no.** A denied or blocked action is final — Moko drops *that
            intent*; it does not try to reach the same end another way. A verbatim
            re-proposal this turn isn't re-prompted (the abandoned-set).
          - **Trust is by boundary class.** "Always" frees the action's *key*
            (network, install, outside_write, …), not the whole shell — and only
            GATE-tier keys; escalation always asks."""
        tool = self._tools[name]
        # A tool may classify each call itself (the shell tiers per command);
        # otherwise the static category/risk decides. `reason`/`tier`/`key` enrich
        # the gate event, the approval prompt, and what "always" trusts.
        reason, tier, key = "", "", tool.category
        if tool.classify is not None:
            decision = tool.classify(args, self._tool_policy)
            verdict, reason, tier, key = (
                decision.verdict, decision.reason, decision.tier, decision.key
            )
        else:
            verdict = consent_decide(tool.category, tool.risk, self._tool_policy)

        def _gate_event(v: str) -> None:
            _publish_event(
                {"type": "gate", "tool": name, "category": tool.category,
                 "verdict": v, "tier": tier, "reason": reason, "key": key}
            )

        if verdict == ALLOW:
            _gate_event("auto-allowed")
            return await self._run_and_log(tool, name, args)
        if verdict == DENY:
            get_logger("brain").info("tool %s denied (%s): %s", name, tier, reason)
            _gate_event("auto-denied")
            refusal = reason or f"That {tool.category} action is switched off"
            # Blocked is a safety floor, not a hurdle to route around.
            return f"(I can't do that — {refusal}. I won't try to work around it.)"

        # ASK: block inline for the companion's yes/no. None → no approver wired.
        if self._request_approval is None:
            return "(That needs your approval, but I can't ask right now.)"
        sig = _action_signature(name, args)
        if sig in self._abandoned:
            # They already declined or moved on from this exact action this turn.
            return "(I've left that one alone, as you indicated.)"
        description = _describe_action(name, args)
        # Append the classify `reason` only when it adds something the per-tool description
        # doesn't already say — else the companion sees "send X to you — send X to you".
        if reason and not (_keywords(reason) and _keywords(reason) <= _keywords(description)):
            description = f"{description} — {reason}"
        payload = json.dumps(
            {"tool": name, "args": args, "description": description, "category": tool.category}
        )
        # Only a GATE-tier, freeable-key action can be remembered with "always".
        always_ok = tier == GATE and key in FREEABLE_KEYS
        decision = await self._request_approval(payload, description, tool.category, always_ok)
        _gate_event(decision)
        if decision in ("approve", "always"):
            # Honour "always" only when the action's class is actually freeable
            # (`always_ok`). The companion can *type* "yes always" for anything, but
            # a non-freeable key (e.g. `author_skill` → self-modification) must stay
            # always-ask: treat it as a one-time approve, never a standing grant.
            if decision == "always" and key and always_ok:
                set_policy(key, "allow")  # trust this boundary class from now on
                self._tool_policy = load_policy()
            return await self._run_and_log(tool, name, args)
        # Anything but yes → set it aside so a re-proposal this turn won't re-ask.
        self._abandoned.add(sig)
        if decision == "redirect":
            return "(Setting that aside — taking your new message instead. I won't re-propose it.)"
        if decision == "timeout":
            return "(No answer yet, so I'll hold off on that for now.)"
        return "(The companion declined — I'll stop this and won't try another way.)"

    async def _run_and_log(self, tool: AgentTool, name: str, args: dict) -> str:
        """Run an approved/allowed action and log the *doings* (commands, file
        writes, skills — not reads), so 'recently you…' carries across turns and
        Moko stops losing track of what it did."""
        result = await tool.run(args)
        if tool.risk == SENSITIVE:
            ok = isinstance(result, str) and "couldn't" not in result.lower()[:40]
            try:
                await self.store.log_action(tool.category, _describe_action(name, args), ok=ok)
            except Exception:
                get_logger("brain").debug("failed to log action", exc_info=True)
        return result

    async def _observe_unremembered_images(self, message_id: str) -> None:
        """Guaranteed gist: for any image on this message Moko hasn't already described
        (no `saw`), spend one focused vision call to record what it shows. So Moko
        remembers what it saw even when the reply turn didn't note it, and even after
        the conversation rotates. Best-effort — a failure just leaves the image
        un-described (the bytes are still on disk to re-examine)."""
        media = await self.store.media_for_message(message_id)
        undescribed = [
            m for m in media if m.kind == "image" and not (m.description or "").strip()
        ]
        if not undescribed:
            return
        parts: list = [
            "Describe what is actually visible in the following image(s), in one or two "
            "plain sentences — just what you see, no preamble or speculation."
        ]
        loaded = []
        for m in undescribed:
            try:
                data = (media_dir() / m.path).read_bytes()
            except OSError:
                continue
            parts.append(ImagePart(data, m.mime or "image/jpeg"))
            loaded.append(m)
        if not loaded:
            return
        usage_kind.set("vision")
        try:
            resp = await self.llm.complete(
                "You observe images and report, plainly and literally, only what is "
                "visible — no guessing beyond the pixels.",
                [Msg("user", parts)],
                max_tokens=150,
            )
            desc = (resp.text or "").strip()
        except Exception:
            get_logger("brain").warning("vision observation backstop failed", exc_info=True)
            return
        if desc:
            for m in loaded:
                await self.store.set_media_description(m.id, desc)

    @staticmethod
    def _append_user_note(msg, note: str) -> None:
        """Append an in-prompt perception note to the latest user turn, whether its
        content is plain text or a multimodal parts list (text-first)."""
        content = msg.content
        if isinstance(content, list):
            if content and isinstance(content[0], str):
                content[0] = (content[0] + note).strip()
            else:
                content.insert(0, note.strip())
        else:
            msg.content = (content + note).strip()

    def _vision_parts(self, text: str, all_media, images) -> list:
        """Build a multimodal user turn: the text + a perception note, then each
        image's bytes as an `ImagePart`. Sync (just disk reads); an image whose blob
        can't be read is skipped, so a missing file degrades to text, never a broken
        turn."""
        others = [a for a in all_media if a.kind != "image"]
        head = (text + _vision_perception(images, others)).strip()
        parts: list = [head] if head else []
        for img in images:
            try:
                data = (media_dir() / img.path).read_bytes()
            except OSError:
                continue
            parts.append(ImagePart(data, img.mime or "image/jpeg"))
        return parts

    async def _companion_place(self) -> str | None:
        """The companion's city for weather (config, else learned, else None) — resolved by
        the weather module, which owns the place/cache logic."""
        cid, _ = await self._ensure_actors()
        return await weather.resolve_place(self.store, self.config, cid)

    async def _current_weather(self) -> weather.Weather | None:
        """Cached current weather for the companion's place (None when off/unavailable).
        Thin bridge to `weather.current`, which owns the cache + fail-TTL."""
        cid, _ = await self._ensure_actors()
        return await weather.current(self.store, self.config, cid)

    # ── outbound attachments (M10) ────────────────────────────────────
    def _make_send_attachment_tool(self) -> AgentTool:
        return AgentTool(
            spec=SEND_ATTACHMENT_TOOL, category="send", risk=SENSITIVE,
            run=self._send_attachment, classify=self._classify_send,
        )

    def _resolve_send_path(self, raw: str) -> Path:
        """Where a send path points: a `~` is expanded, absolute paths are taken as given,
        and a relative name resolves under Moko's own media folder (so the model can name a
        stored artifact directly). Expanding `~` matters — the model often writes
        `~/.moko/media/<file>`, which without expansion is treated as relative and joined
        under media_dir into a bogus path that doesn't exist (the cause of failed sends)."""
        p = Path(raw).expanduser()
        return p if p.is_absolute() else media_dir() / p

    def _classify_send(self, args: dict, policy: dict[str, str]) -> GateDecision:
        """Free to send a file from Moko's own media folder to the companion (their
        data, their channel); ASK for anything outside it — sending an arbitrary local
        file off-box is exfil-class (safety floor #3), and it's never blanket-trusted
        (the key isn't freeable), so it always asks."""
        path = self._resolve_send_path(str(args.get("path", "")).strip())
        try:
            resolved, media_root = path.resolve(), media_dir().resolve()
        except OSError:
            return GateDecision(ASK, reason="send a file to you", tier=GATE, key="send_file")
        if resolved == media_root or media_root in resolved.parents:
            return GateDecision(ALLOW, key="")  # Moko's own artifact → straight through
        return GateDecision(ASK, reason=f"send {path.name} to you", tier=GATE, key="send_file")

    async def _send_attachment(self, args: dict) -> str:
        """Send a file to the companion on the current channel, recording an outbound,
        sourced media artifact. Honest, bounded failures — a missing/oversize file or
        a channel that can't send is reported, not faked."""
        if self._send_file is None:
            return "I can't send files on this channel."
        path = self._resolve_send_path(str(args.get("path", "")).strip())
        caption = args.get("caption")
        try:
            data = path.read_bytes()
        except OSError:
            get_logger("brain").warning("send_attachment: no file at %s", path)
            return f"I couldn't find a file at {str(args.get('path', ''))!r} to send."
        if len(data) > _MAX_SEND_BYTES:
            return f"That file is too large to send ({len(data) // (1024 * 1024)} MB)."
        kind = _media_kind_for(path)
        mime = mimetypes.guess_type(path.name)[0]
        # Privacy floor: scrub EXIF (GPS, camera, capture time) from an outbound image
        # before it leaves the machine — re-sharing a photo must never leak where it was
        # taken. Fail-CLOSED: strip, then verify the result is clean and REFUSE to send
        # if any metadata survived (a piexif failure on a JPEG, or a PNG carrying an eXIf
        # chunk). Record what actually went out (the stripped bytes).
        if kind == "image":
            data = exif.strip_exif(data, mime)
            if exif.has_exif(data, mime):
                get_logger("brain").warning("refusing to send %s — couldn't scrub its metadata",
                                            path.name)
                return (f"I couldn't safely strip the location/metadata from {path.name}, "
                        "so I won't send it.")
        # Record what Moko sent: a sourced, outbound artifact (so it's auditable and Moko
        # remembers having sent it). But a file Moko *generated* is already on record (it was
        # add_media'd at creation) — recording it again on send just double-counts it. Dedup
        # only against OUTBOUND rows, so re-sharing a file that arrived *inbound* still gets
        # its own outbound provenance.
        sha = blobs.sha256_hex(data)
        existing = await self.store.media_by_sha(sha, direction="out")
        if existing is None:
            existing = await self.store.add_media(
                data, kind=kind, mime=mime, filename=path.name, caption=caption,
                direction="out", channel=self._reply_channel,
            )
        try:
            await self._send_file(data=data, filename=path.name, caption=caption, kind=kind)
        except Exception:
            get_logger("brain").warning("send_file failed", exc_info=True)
            return f"I tried to send {path.name}, but the channel wouldn't take it."
        self._sent_attachment = True  # a file went out — the turn isn't dead air
        # Surface the sent file on the feed (with a thumbnail deep-link to its id).
        _publish_event({"type": "media.sent", "id": existing.id, "media_kind": kind,
                        "caption": caption, "filename": path.name})
        if caption and caption.strip():
            # Remember the caption so the turn's reply doesn't just restate it (the
            # companion already read it under the file — repeating is unnatural).
            self._sent_captions.append(caption.strip())
        return f"Sent {path.name} to you."

    async def _close_answered(self, numbers, gap_by_number: dict[int, str]) -> None:
        """Resolve the open questions the model said the companion just answered —
        the semantic gap resolver. `numbers` are the displayed question numbers; the
        model understands what was answered, so this is just the bookkeeping."""
        for n in numbers or []:
            num = n if isinstance(n, int) else None
            if isinstance(n, str) and n.strip().isdigit():
                num = int(n.strip())
            gid = gap_by_number.get(num) if num is not None else None
            if gid:
                # resolved_by is an FK (a source/message), so leave it null.
                await self.store.set_gap_status(gid, "resolved")

    async def _salvage(
        self, system, user_text: str, inbound_id: str, channel: str, companion_id: str,
        touched: list[str],
    ) -> bool:
        """Second-chance extraction: when the model replied without recording an
        info-rich turn, run one focused call with only the `remember` tool to
        capture what was missed. Best-effort; returns whether it recorded."""
        fired = False

        async def execute(name: str, args: dict) -> str:
            nonlocal fired
            # On the forced path a weak brain may call `remember` empty (we told it
            # to, when there's nothing). Only count + apply a call that actually
            # carries something, so an empty forced call neither writes a source nor
            # flips `recorded` (which would trigger a needless gap rescan).
            if name == REMEMBER_TOOL.name and not extraction_from_obj(args).is_empty:
                fired = True
                return await self._remember(args, inbound_id, channel, companion_id, touched)
            return ""

        # A weaker brain skips the tool even when there IS something to record, so
        # for it we force the `remember` call (and tell it to record nothing by
        # calling `remember` empty). A capable brain stays elective — it calls when
        # there's something and correctly declines when there isn't, so don't force
        # it (forcing would only invite a contentless or invented call).
        force = getattr(self.llm, "needs_scaffolding", False)
        decline = "call `remember` with no fields" if force else "don't call it"
        ask = Msg(
            "user",
            "From the companion's last message, record anything worth remembering by calling "
            "`remember` (follow your remembering skill — interests and tastes count). If there's "
            f"genuinely nothing, {decline}.\n\nTheir message: {user_text!r}",
        )
        usage_kind.set("reply")  # salvage is part of the reply pipeline
        try:
            await self.llm.converse(
                system, [ask], tools=[REMEMBER_TOOL], execute=execute, max_steps=2,
                tool_choice=REMEMBER_TOOL.name if force else None,
            )
        except Exception:
            get_logger("brain").warning("salvage extraction failed", exc_info=True)
        return fired

    async def _add_gap_deduped(
        self,
        description: str,
        kind: str,
        existing_gaps: list[str],
        *,
        resolution_path: str = "either",
        about_id: str | None = None,
    ) -> bool:
        """Add a gap unless a near-identical one is already queued. Keeps
        `existing_gaps` in sync so successive callers dedup against it too.
        The single choke point for every gap Moko opens during a turn."""
        if not description or any(_similar_gap(description, prior) for prior in existing_gaps):
            return False
        await self.store.add_gap(
            description, kind, about_id=about_id, resolution_path=resolution_path
        )
        existing_gaps.append(description)
        return True

    async def _verify(self, marker: str, about_id: str | None, existing_gaps: list[str]) -> None:
        """Open a `verification` gap for a contradiction worth checking later. The
        marker is an internal label; the actual question is phrased at ask-time
        (Phase C), in Moko's voice and the companion's language."""
        await self._add_gap_deduped(
            marker, "verification", existing_gaps, resolution_path="ask", about_id=about_id
        )

    async def _supersede_functional(
        self, from_id: str, rel_type: str, new_to_id: str, existing_gaps: list[str],
        *, verify: bool = True,
    ) -> None:
        """A single-valued relationship got a new target: close any conflicting
        current edge (kept in history). Open a verification gap only when the
        change came from a non-authoritative source — when the companion stated
        it themselves there's nothing to double-check with them."""
        for rel in await self.store.relationships_for(from_id):
            if rel.from_id != from_id or rel.type != rel_type or rel.to_id == new_to_id:
                continue
            await self.store.end_relationship(rel.id)
            if not verify:
                continue
            who = await self.store.get_subject(from_id)
            old = await self.store.get_subject(rel.to_id)
            new = await self.store.get_subject(new_to_id)
            await self._verify(
                f"verify: {who.name if who else '?'} {rel_type.replace('_', ' ')}"
                f" {new.name if new else '?'} now (was {old.name if old else '?'})",
                from_id,
                existing_gaps,
            )

    def _parse_local_dt(self, raw: str | None) -> dt.datetime | None:
        """Parse the model's local 'YYYY-MM-DD[ HH:MM[:SS]]' into a UTC datetime (the model
        computes it from the current date+time in its context). None when empty or
        unparseable; never raises. Single-sourced so a commitment's due time and an
        observation time accept exactly the same formats."""
        if not raw:
            return None
        text = str(raw).strip().replace("T", " ")
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
            try:
                naive = dt.datetime.strptime(text, fmt)
            except ValueError:
                continue
            return self.clock.to_utc(naive)
        return None

    def _parse_due(self, due_local: str | None) -> str | None:
        """Turn the model's local due time into a stored UTC instant. Guarded — a due
        time landing well in the past is almost certainly a hallucinated date, so we drop
        it to a someday commitment rather than let it fire the instant it's created.
        Unparseable → None too, never raises."""
        when = self._parse_local_dt(due_local)
        if when is None:
            if due_local:  # non-empty but unparseable (empty is a silent no-op)
                get_logger("brain").info("unparseable commitment due_at: %r", due_local)
            return None
        if when < now_utc() - dt.timedelta(hours=COMMITMENT_PAST_DUE_GRACE_HOURS):
            get_logger("brain").info("commitment due_at in the past, dropping: %r", due_local)
            return None
        return when.isoformat()

    def _parse_observed(self, when_local: str | None) -> str | None:
        """A model-supplied local time for when a datapoint is about (e.g. last night) →
        stored UTC. Unlike a commitment's due_at, the past is expected — you measure things
        that already happened — so there's no past-due drop. Unparseable → None (caller
        defaults to the turn's source time)."""
        when = self._parse_local_dt(when_local)
        return when.isoformat() if when is not None else None

    async def _thought_subject(
        self, about, names: dict[str, str], companion_id: str, companion_aliases: set[str]
    ) -> str | None:
        """Resolve what a thought is *about* to an existing subject id, for linking
        — never creating one. Prefers subjects already touched this turn, falls
        back to a name lookup, and maps self-references to the companion. Returns
        None for an unknown or bare-role subject (no junk nodes from a thought)."""
        name = (about or "").strip()
        low = name.lower()
        if not name or low in _RESERVED_SUBJECT_NAMES or low in _GENERIC_ROLES:
            return None
        if low in companion_aliases:
            return companion_id
        if low in names:
            return names[low]
        found = await self.store.find_by_name(name)
        return found.id if found else None

    async def _apply(
        self,
        extraction: Extraction,
        inbound_id: str | None,
        channel: str | None,
        companion_id: str,
        touched: list[str],
        *,
        source_kind: str = "message",
        source_ref: str = "",
    ) -> None:
        # An empty payload (e.g. a forced `remember` on a turn with nothing to
        # record) is a no-op: never write a source row with no facts attached —
        # that pollutes the provenance layer that's the whole point of Moko.
        if extraction.is_empty:
            return
        # `message` for a conversation turn; `web` when Moko researched it itself
        # (no inbound message, so no sentiment to attach). `source_ref` is the
        # message id or, for web, the page URL / search the fact came from.
        ref = source_ref or (inbound_id or "")
        source = await self.store.add_source(source_kind, ref=ref, channel=channel)
        names: dict[str, str] = {}
        # Open gaps, fetched once and shared by every gap-opening site below
        # (model gaps, contradictions, unnamed roles, possession) so none of
        # them pile up a rephrasing of a question already in the queue.
        existing_gaps = [g.description for g in await self.store.open_gaps(limit=50)]

        # Self-references resolve to the known companion, never a new node —
        # so "my friend Mia" / "my dog Biscuit" link to the real person,
        # not a junk "speaker" subject (and stay findable across sessions).
        companion_aliases = {"speaker", "the speaker", "i", "me", "myself", "user", "the user"}
        cname = (self.config.companion_name or "").strip().lower()
        if cname:
            companion_aliases.add(cname)

        # Unnamed people mentioned only by role ("my brother") — collected so we
        # can ask their name later instead of minting a junk subject for them.
        unnamed_roles: set[str] = set()

        async def resolve_name(name: str, default_kind: str = "person") -> str | None:
            name = _clean_subject_name(name or "")
            low = name.lower()
            if not name or low in _RESERVED_SUBJECT_NAMES:
                return None
            if low in companion_aliases:
                return companion_id
            if low in _GENERIC_ROLES:
                unnamed_roles.add(low)
                return None
            if low in names:
                return names[low]
            found = await self.store.find_by_name(name)
            eid = found.id if found else (await self.store.add_subject(default_kind, name)).id
            names[low] = eid
            touched.append(eid)
            return eid

        created: list[tuple[str, str, str]] = []  # (id, kind, name)
        for ent in extraction.subjects:
            name = _clean_subject_name(ent.get("name") or "")
            low = name.lower()
            if not name or low in _RESERVED_SUBJECT_NAMES or low in companion_aliases:
                continue
            if low in _GENERIC_ROLES:
                unnamed_roles.add(low)  # a role, not a name — ask for it later
                continue
            subject = await self.store.add_subject(ent.get("kind") or "topic", name)
            names[low] = subject.id
            touched.append(subject.id)
            created.append((subject.id, subject.kind, subject.name))

        for attr in extraction.attributes:
            key = (attr.get("key") or "").strip()
            value = attr.get("value")
            eid = await resolve_name(attr.get("subject", ""))
            if not (eid and key and value is not None):
                continue
            # Don't store a non-answer as a fact — that's a gap, not an attribute.
            if any(vague in str(value).lower() for vague in _VAGUE_VALUES):
                continue
            # A named thing is never a scalar attribute value — it's a
            # relationship to its own subject. Two cases: the value already names
            # a subject, or the key implies a place. Split compound place values
            # ("Victoria, Australia") into separate subjects; require proper
            # nouns for new ones so a non-geographic "state=anxious" stays scalar.
            val = str(value).strip()
            low_key = key.lower()
            existing = await self.store.find_by_name(val)
            # Convert only when it's unambiguous: the value already names an
            # subject, or the key is clearly geographic. No capitalization gate —
            # ambiguous keys are left for the dream's LLM reclassification.
            geo_key = "capital" in low_key or any(t in low_key for t in _UNAMBIGUOUS_GEO_TERMS)
            parts = [existing.name] if existing else (_split_places(val) if geo_key else [])
            if parts:
                rel = _attr_relation(low_key)
                conf = _as_unit(attr.get("confidence")) or 0.8
                for part in parts:
                    vid = await resolve_name(part, existing.kind if existing else "place")
                    if vid and vid != eid:
                        await self.store.add_relationship(
                            eid, vid, rel, source_id=source.id, confidence=conf
                        )
                        touched.append(vid)
                get_logger("brain").info("attribute %s=%r → relationship(s)", key, val)
                continue
            value_type = attr.get("value_type") or "string"
            confidence = _as_unit(attr.get("confidence")) or 0.8
            if attr.get("prior"):
                # A value that was true in the PAST and has already ended ("she used to be a
                # receptionist"): record it as history of the SAME key, slotted before the
                # current value — never a parallel `previous_X` key.
                await self.store.add_past_attribute(
                    eid, key, str(value), source_id=source.id,
                    value_type=value_type, confidence=confidence,
                )
            else:
                # Supersede-with-history (set_attribute closes the old value). Whether
                # a change is a real contradiction worth verifying — vs. a refinement
                # or correction — is judged by the dream's LLM, not a write-time
                # string check.
                await self.store.set_attribute(
                    eid, key, str(value), source_id=source.id,
                    value_type=value_type, confidence=confidence,
                )

        # Observations: dated datapoints in a series (sleep, weight, a run), the
        # append-only twin of attributes — each reading sits beside the last
        # rather than overwriting it. The date lives in observed_at (resolved
        # local→UTC), never in the key, so "today" is never baked into a string.
        for obs in extraction.observations:
            key = (obs.get("key") or "").strip()
            value = obs.get("value")
            eid = await resolve_name(obs.get("subject", ""))
            if not (eid and key and value is not None):
                continue
            if any(vague in str(value).lower() for vague in _VAGUE_VALUES):
                continue
            await self.store.add_observation(
                eid,
                key,
                str(value),
                source_id=source.id,
                value_type=obs.get("value_type") or "string",
                observed_at=self._parse_observed(obs.get("observed_at")) or source.observed_at,
            )

        for rel in extraction.relationships:
            rel_type = (rel.get("type") or "").strip()
            from_id = await resolve_name(rel.get("from", ""))
            to_id = await resolve_name(rel.get("to", ""))
            if not (rel_type and from_id and to_id):
                continue
            # Functional edges (lives_in, works_at…) hold one current value: a
            # new target supersedes the old (kept in history). Multi-valued edges
            # (friend_of…) just accumulate. A verify gap only opens when the
            # change isn't from the companion's own word (they don't need asking).
            if rel_type in _FUNCTIONAL_RELS:
                await self._supersede_functional(
                    from_id, rel_type, to_id, existing_gaps,
                    verify=source_kind not in _COMPANION_SOURCES,
                )
            await self.store.add_relationship(
                from_id,
                to_id,
                rel_type,
                source_id=source.id,
                directed=bool(rel.get("directed", True)),
                confidence=_as_unit(rel.get("confidence")) or 0.8,
            )

        sentiment = extraction.speaker_sentiment
        if sentiment and sentiment.get("label") and inbound_id is not None:
            await self.store.set_message_sentiment(
                inbound_id,
                sentiment=sentiment.get("label"),
                valence=_as_valence(sentiment.get("valence")),
                arousal=_as_unit(sentiment.get("arousal")),
                confidence=_as_unit(sentiment.get("confidence")),
            )

        for thought in extraction.thoughts:
            body = (thought.get("body") or "").strip()
            if not body:
                continue
            # Link the thought to what it's about, so `lookup`/`reflect` can read
            # it back later. Resolve against subjects already known/touched this
            # turn or the companion — never *mint* a node just for a thought
            # (that's how junk subjects creep in).
            # Provenance: link every thought to the source that prompted it, so
            # "why does Moko think this?" is answerable (the source row is already
            # in hand). Plus the subject it's about, when resolvable.
            links = [("source", source.id)]
            aid = await self._thought_subject(
                thought.get("about"), names, companion_id, companion_aliases
            )
            if aid:
                links.append(("subject", aid))
            # How Moko feels about it (-1..1), and the flashbulb effect: a
            # strongly-felt thought gets higher initial stability, so it decays
            # slower and stays prominent. Neutral notes keep the default.
            valence = _as_valence(thought.get("valence"))
            await self.store.add_thought(
                thought.get("kind") or "note",
                body,
                valence=valence,
                stability=_stability_from_valence(valence),
                shareability=thought.get("shareability") or "private",
                links=links,
            )

        # The model's own gaps, deduped against everything already queued.
        for gap in extraction.gaps:
            # Link the gap to its subject subject (when known), so it can be resolved
            # and topic-selected later instead of floating with a NULL about_id —
            # the bug that left answered curiosities open. Resolve only to existing
            # subjects (never mint one for a gap).
            gap_about = await self._thought_subject(
                gap.get("about"), names, companion_id, companion_aliases
            )
            await self._add_gap_deduped(
                (gap.get("description") or "").strip(),
                gap.get("kind") or "open_curiosity",
                existing_gaps,
                resolution_path=gap.get("resolution_path") or "either",
                about_id=gap_about,
            )

        # Commitments — things Moko has agreed to do or remind. This is what makes
        # "remind me tomorrow" real instead of a hollow "noted": the heartbeat
        # surfaces these when `due_at` passes. Recording fails closed (a bad due
        # time becomes a no-fixed-time commitment, never a dropped one).
        #
        # Deduped on near-identical description AND the same due time, so a genuine
        # repeat at a *different* time ("remind me to stretch at 3 and at 5") still
        # lands. Broadened past still-open to commitments created in the last ~2 days
        # (any status), so re-promising the same reminder right after the first was
        # surfaced+closed doesn't respawn it. (The morning self-portrait pile-up is
        # prevented at the root by the capability note above — Moko no longer promises
        # what it can't do — so the staggered dupes are never created in the first place.)
        prior = [(c.description, c.due_at) for c in await self.store.open_commitments()]
        prior += [
            (c.description, c.due_at)
            for c in await self.store.recent_commitments(within_hours=48)
        ]

        def _is_duplicate(desc: str, due: str | None) -> bool:
            return any(
                due == prior_due and _similar_gap(desc, prior_desc, threshold=0.8)
                for prior_desc, prior_due in prior
            )

        for c in extraction.commitments:
            desc = (c.get("description") or "").strip()
            if not desc:
                continue
            due_at = self._parse_due(c.get("due_at"))
            if _is_duplicate(desc, due_at):
                get_logger("brain").info("skipping duplicate commitment: %r", desc)
                continue
            about = await self._thought_subject(
                c.get("about"), names, companion_id, companion_aliases
            )
            await self.store.add_commitment(
                desc,
                kind=c.get("kind") or "reminder",
                about_id=about,
                due_at=due_at,
                source_id=source.id,
            )
            prior.append((desc, due_at))  # dedup within one payload too

        # Unnamed relatives/roles → a gap to learn the actual name (ask-path), so
        # a bare "my brother" becomes a gentle question later, never a junk
        # subject. (Internal marker; the question itself is phrased at ask-time.)
        for role in unnamed_roles:
            await self._add_gap_deduped(
                f"the speaker's {role}'s name", "missing_attr", existing_gaps, resolution_path="ask"
            )

        # Possession backstop: a pet/object got recorded but the model didn't
        # link it to anyone. Don't assume it's the companion's — open a gap so
        # Moko asks whose it is later ("Who owns Biscuit?").
        for eid, kind, name in created:
            if kind not in _POSSESSABLE_KINDS:
                continue
            rels = await self.store.relationships_for(eid)
            if any(companion_id in (r.from_id, r.to_id) for r in rels):
                continue  # already linked to the companion
            question = f"Who owns {name}?" if kind == "pet" else f"Who does {name} belong to?"
            await self._add_gap_deduped(
                question, "unconfirmed_rel", existing_gaps, resolution_path="ask", about_id=eid
            )
