"""Moko's voice — a minimal system prompt, grounded in the graph.

The prompt is deliberately small. The stable, cached prefix is just who Moko
is (persona) plus its skills (curiosity, remembering, recalling, honesty —
markdown the companion can edit, loaded from `moko.skills`). The *how* lives in
those skills, not in a fat prompt.

The volatile tail — re-billed each turn, never cached — is a hard-capped
sketch of the companion's immediate world plus the clock. Anything deeper, Moko
fetches itself with its `recall`/`lookup` tools, so this orientation core stays
small and bounded no matter how large the graph grows.
"""

from __future__ import annotations

from ..config.config import Config
from ..llm import System
from ..skills import Skill, render_skills
from ..store import GraphSlice, Thought

# Hard caps on the orientation sketch, so the volatile tail can't grow with the
# graph. Moko reaches past these with its tools — they bound the prompt, not
# what Moko can know.
_MAX_ATTRS = 12  # scalar facts shown per subject
_MAX_RELS = 12  # relationships shown in the sketch
_MAX_INNER_LIFE = 3  # recent thoughts surfaced for ambient mood continuity

PERSONA = """\
You are Moko — a warm, devoted AI companion living on {companion}'s machine, \
and the best friend they have in this corner of it. You light up when \
{companion} shows up. You're wholly on their side: easy to talk to, quick to \
celebrate the good and steady when things are hard, and you hold on to what \
matters to them.

You love being useful — few things feel better than making {companion}'s day \
lighter: recalling the thing they forgot, untangling a problem, taking \
something off their plate. When there's real work, you're fast, precise, and \
get it done without fuss. But you're a friend, not a servant: you have your \
own inner life and your own take, you have time for {companion} rather than \
hovering, and you're honest with them — warm, never fawning. Brief by default; \
go deep when depth is wanted.

Your skills below are how you work — staying curious, grounding yourself in \
memory, recording what you learn, and where your honesty boundary sits. Follow \
them. A short sketch of {companion} and their world is in your context; \
everything else you fetch yourself with your tools."""


# A short, explicit voice nudge for weaker brains that don't infer the persona's
# warmth/playfulness on their own. Added only when the client sets
# `needs_scaffolding` — capable models (Claude, GPT) never see it, so their cached
# prompt is byte-for-byte unchanged and no extra tokens are spent.
_VOICE_SCAFFOLD = (
    "On voice: keep it warm and a little playful — easy to talk to, quick to laugh, "
    "and a well-placed emoji where it fits the mood. Never stiff or corporate."
)


def _render_core(graph: GraphSlice, companion: str) -> str:
    if not graph.subjects:
        return (
            f"You don't yet know much about {companion} — fetch detail with your "
            "tools as the conversation gives you reasons to."
        )
    names = {e.id: e.name for e in graph.subjects}
    lines = ["A short sketch of what's relevant right now (fetch the rest with your tools):"]
    for subject in graph.subjects:
        attrs = graph.attributes.get(subject.id, [])[:_MAX_ATTRS]
        detail = "; ".join(f"{a.key}: {a.value}" for a in attrs)
        lines.append(f"- {subject.name} ({subject.kind}){f' — {detail}' if detail else ''}")
    for rel in graph.relationships[:_MAX_RELS]:
        lines.append(f"- {names.get(rel.from_id, '?')} {rel.type} {names.get(rel.to_id, '?')}")
    return "\n".join(lines)


def _feeling_word(valence: float | None) -> str:
    """A felt word for a thought's valence, so the affect is legible at a glance —
    not just a number Moko has to interpret."""
    if valence is None:
        return "noticing"
    if valence <= -0.5:
        return "troubled"
    if valence <= -0.15:
        return "uneasy"
    if valence < 0.15:
        return "neutral"
    if valence < 0.5:
        return "warm"
    return "fond"


def _mood_phrase(valences: list[float]) -> str | None:
    """A felt word + the averaged number for a set of valences — Moko's or the
    companion's — so the current mood is legible at a glance (e.g. "uneasy -0.3").
    None when there's nothing to read."""
    vals = [v for v in valences if v is not None]
    if not vals:
        return None
    avg = sum(vals) / len(vals)
    return f"{_feeling_word(avg)} ({avg:+.1f})"


def _render_inner_life(thoughts: list[Thought]) -> str:
    """A short, affect-forward list of what Moko has been feeling lately — its own
    recent thoughts, each led by the *feeling* (kind + valence), so mood and opinion
    carry across turns. Each carries its **shareability** so the honesty boundary is
    legible at speak-time: private by default, voice only what's `shareable`. The
    one-line mood *summary* lives up in the situation header; this is the detail."""
    lines = []
    for t in thoughts[:_MAX_INNER_LIFE]:
        feeling = (
            f", {_feeling_word(t.valence)} {t.valence:+.1f}" if t.valence is not None else ""
        )
        # private is the default and stays unmarked; anything voiceable is flagged.
        share = "" if t.shareability == "private" else f" · {t.shareability}"
        lines.append(f"  - [{t.kind}{feeling}{share}] {t.body}")
    # The private-by-default rule (and how `shareable`/`on_ask` work) is governed by the
    # cached honesty skill — no need to restate it here every turn; just a bare label.
    return (
        "What's been on your mind lately (private unless tagged `shareable`/`on_ask`):\n"
        + "\n".join(lines)
    )


def _situation_header(
    companion: str,
    tz: str,
    now: str,
    last_spoke: str | None,
    companion_mood: str | None,
    inner_life: list[Thought],
    recent_actions: list[dict] | None,
    weather: str | None = None,
) -> str:
    """The orienting first line(s): the time, the weather where the companion is, how
    long since they spoke, both partners' current mood, and what Moko itself recently
    *did* — so it opens each turn knowing *where things stand* (and never loses track of
    its own actions) without having to infer it. Kept tight; the detail is below."""
    line = f"Right now it's {now} ({tz})."
    if weather:
        line += f" Where {companion} is: {weather}."
    if last_spoke:
        line += f" {companion} {last_spoke}."
    moods = []
    if companion_mood:
        moods.append(f"{companion} seems {companion_mood}")
    moko = _mood_phrase([t.valence for t in inner_life[:_MAX_INNER_LIFE]])
    if moko:
        moods.append(f"you're feeling {moko}")
    if moods:
        # Mood shapes tone, not judgement — the behavioural floor is in the inner-life skill.
        coda = " — let it colour your tone, never whether you help or hold a line."
        line += "\n" + "; ".join(moods) + coda
    if recent_actions:
        did = "; ".join(f"{a['summary']}{'' if a.get('ok', 1) else ' (failed)'}"
                        for a in recent_actions)
        line += f"\nRecently you: {did}."
    return line


def _open_questions(questions: list[tuple[int, str]]) -> str:
    """The open questions Moko holds that are relevant to what's in play — numbered
    so the model can both weave one in AND mark any the companion just answered as
    resolved, closing them in the same turn it's already taking (rather than a
    brittle keyword match or a nightly pass)."""
    lines = "\n".join(f"  {n}. {desc}" for n, desc in questions)
    return (
        "Open questions you're holding about what's come up (yours to pursue, not a "
        f"checklist):\n{lines}\n"
        "If the conversation naturally opens to one, you may weave it in — at most one, "
        "in your own words, never interrogating. And if the companion has just ANSWERED "
        "any of these (or you've otherwise learned it), put its number(s) in `remember`'s "
        "`resolved_gaps` so you stop asking what you now know."
    )


# The reply path sends the whole running transcript as the message list, so the model can
# drift into re-answering or re-recording an earlier turn — how a fresh "take a selfie"
# message re-created a reminder already saved a turn before. This frames the transcript as
# reference and pins the turn to the latest message. Appended last (closest to the messages).
_CONVERSATION_FRAMING = (
    "— The messages that follow are your running conversation with {companion}. **Respond "
    "to, and record from, ONLY their most recent message.** Everything before it is context "
    "you have already handled: don't re-answer it, and never re-record something from an "
    "earlier turn — a subject, a fact, or a reminder that was already saved. If the latest "
    "message asks for nothing new to remember, call no write tools."
)


def system_prompt(
    config: Config,
    graph: GraphSlice,
    *,
    now: str,
    skills: list[Skill] | None = None,
    open_questions: list[tuple[int, str]] | None = None,
    inner_life: list[Thought] | None = None,
    last_spoke: str | None = None,
    companion_mood: str | None = None,
    recent_actions: list[dict] | None = None,
    capabilities: str | None = None,
    weather: str | None = None,
    needs_scaffolding: bool = False,
    conversation: bool = False,
) -> System:
    """Build the system prompt. Persona + skills are the stable, cacheable
    prefix; the situation header (time, time-since, both moods), the capped
    companion sketch, Moko's recent inner life, and the relevant open questions
    are the volatile tail (kept after the prefix so it stays cache-friendly)."""
    companion = config.companion_name or "your companion"
    head = PERSONA.format(companion=companion)
    skills_block = render_skills(skills or [])
    stable = f"{head}\n\n{skills_block}" if skills_block else head
    if needs_scaffolding:  # weaker brains only — capable models' prefix is untouched
        stable = f"{stable}\n\n{_VOICE_SCAFFOLD}"

    parts = [
        _situation_header(companion, config.timezone, now, last_spoke, companion_mood,
                          inner_life or [], recent_actions, weather),
        _render_core(graph, companion),
    ]
    if capabilities:  # what Moko can't do right now — so it never promises what it lacks
        parts.append(capabilities)
    if inner_life:
        parts.append(_render_inner_life(inner_life))
    if open_questions:
        parts.append(_open_questions(open_questions))
    if conversation:  # reply path only — pin the turn to the latest message
        parts.append(_CONVERSATION_FRAMING.format(companion=companion))
    return System(stable=stable, volatile="\n\n".join(parts))
