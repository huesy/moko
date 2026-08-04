"""The dream — Moko's nightly consolidation.

Once per night, in the companion's dream window, Moko tidies its mind instead of
idling: it closes the day's working session (waking fresh), lets old private
thoughts fade (prudence decay, never deletion), re-scores unattended curiosity so
it can't pile up, reconciles likely-duplicate subjects, and leaves a short
journal of what it consolidated and now wonders about.

Safe and auditable by construction: nothing is deleted (decay has a floor, merges
keep an audited, reversible record), and it never self-modifies. Runs once per
night: a crash — handled, or a hard kill that leaves the run unfinished — does NOT
re-run tonight (which would double-apply the once-a-night decays); it resumes in
the next window, and a leftover in-progress row is tidied when the next dream begins.
"""

from __future__ import annotations

import datetime as dt
import re

from ..clock import now_utc
from ..llm import Msg
from ..log import get_logger
from ._json import json_between
from .curiosity import detect_gaps
from .proactivity import ProactivityGovernor
from .rhythms import dream_gate
from .usage import LLM_IO_RETENTION_DAYS, usage_kind

_log = get_logger("dream")

# Reflection tunables.
_PRUDENCE_RATE = 0.1  # nightly prudence decay, slowed by a thought's stability
_PRUDENCE_FLOOR = 0.05  # a faded thought is kept at this weight — never forgotten
_GAP_DECAY = 0.9  # open-gap salience multiplier per night
_GAP_FLOOR = 0.05
_MAX_RECONCILE = 20  # cap reconciliation LLM calls per night
_NIGHTLY_LLM_BATCH = 30  # max graph items folded into any one nightly LLM-review pass


async def _rotate(store, key: str, items: list, size: int) -> list:
    """Take the next `size`-item window of `items` across nights, advancing a
    persisted cursor and wrapping at the end. The cleanup passes are LLM-cost-
    capped per night; without rotation they'd review only the first `size` rows
    *forever* and silently stop covering the graph as it grows. Now the whole
    graph is eventually reviewed, a window per night."""
    if len(items) <= size:
        return items  # fits in one night — no paging needed
    offset = int(await store.get_state(key) or 0)
    if offset >= len(items):
        offset = 0
    window = items[offset : offset + size]
    nxt = offset + size
    await store.set_state(key, str(0 if nxt >= len(items) else nxt))
    _log.info("dream: %s reviewing %d-%d of %d", key, offset, offset + len(window), len(items))
    return window


async def should_dream(brain, now: dt.datetime) -> bool:
    """Dream *repeatedly* through the night: inside the sleep window, not while the
    companion is mid-conversation (the dream ends the session), and spaced by
    DREAM_INTERVAL_MINUTES since the last dream tonight. The first dream of the window
    also runs the once-a-night decays; later ones do the repeatable consolidation +
    curiosity work (dream() guards the decays). Spacing on the last dream's *start*
    also handles crash safety — a hard-killed dream won't re-run until the interval
    passes, and the decays are first-of-night-only regardless."""
    gov = ProactivityGovernor(brain.config)
    # `now` is local; the gate compares against stored UTC timestamps, so hand it both. The
    # eligibility floor is single-sourced in rhythms.dream_gate (shared with the monitor).
    gate = await dream_gate(brain.store, gov, brain.clock, now, brain.clock.to_utc(now))
    return gate.eligible


async def _subject_brief(store, eid: str) -> str:
    """A compact summary of a subject's current facts + one hop of edges, so the
    reconcile judge decides on the same evidence a human would — not a bare
    name+kind (which is exactly the ambiguous case, e.g. "Mercury" the planet vs
    the deity vs a person). Bounded so the prompt stays small."""
    attrs = await store.attributes_for(eid)
    facts = "; ".join(f"{a.key}={a.value}" for a in attrs[:6])
    edges = []
    for r in (await store.relationships_for(eid))[:6]:
        other = await store.get_subject(r.to_id if r.from_id == eid else r.from_id)
        edges.append(f"{r.type} {other.name if other else '?'}")
    parts = []
    if facts:
        parts.append(f"facts: {facts}")
    if edges:
        parts.append(f"links: {', '.join(edges)}")
    return " | ".join(parts) if parts else "(no other details recorded yet)"


async def _judge_same(brain, candidate: dict) -> str:
    """Ask the model whether two flagged subjects are the same real thing.
    Returns SAME / DIFFERENT / UNSURE — defaulting to UNSURE on anything
    unparseable, so the dream never merges on a shaky read."""
    a_brief = await _subject_brief(brain.store, candidate["a_id"])
    b_brief = await _subject_brief(brain.store, candidate["b_id"])
    prompt = (
        "Two entries in a personal memory graph might refer to the same real-world thing:\n"
        f"  A: {candidate['a_name']} (kind: {candidate['a_kind']}) — {a_brief}\n"
        f"  B: {candidate['b_name']} (kind: {candidate['b_kind']}) — {b_brief}\n"
        f"Evidence: {candidate['evidence']}\n"
        "Are A and B the same subject? Reply with exactly one word: SAME, DIFFERENT, or UNSURE."
    )
    try:
        resp = await brain.llm.complete(
            "You reconcile a memory graph. Be conservative — when in doubt, say UNSURE.",
            [Msg("user", prompt)],
            max_tokens=8,
        )
    except Exception:
        _log.warning("reconcile judge failed; treating as UNSURE", exc_info=True)
        return "UNSURE"
    word = (resp.text or "").strip().upper()
    for verdict in ("SAME", "DIFFERENT", "UNSURE"):
        if verdict in word:
            return verdict
    return "UNSURE"


async def _reconcile(brain) -> dict:
    """Review flagged duplicate subjects with the model: merge clear matches
    (non-destructively, audited), reject clear non-matches, and ask the companion
    only when genuinely unsure. Bounded per night."""
    store = brain.store
    candidates = await store.open_merge_candidates()
    merged = rejected = deferred = 0
    for c in candidates[:_MAX_RECONCILE]:
        try:  # one bad candidate must not abort the whole night's reconciliation
            verdict = await _judge_same(brain, c)
            if verdict == "SAME":
                await store.merge_subjects(
                    c["a_id"], c["b_id"], by="dream",
                    reason=f"same as {c['b_name']}: {c['evidence']}",
                )
                await store.set_merge_candidate_status(c["id"], "confirmed")
                merged += 1
            elif verdict == "DIFFERENT":
                await store.set_merge_candidate_status(c["id"], "rejected")
                rejected += 1
            else:  # UNSURE → hand it to the companion, never guess
                await store.add_gap(
                    f"is {c['a_name']} ({c['a_kind']}) the same as {c['b_name']} ({c['b_kind']})?",
                    "verification",
                    resolution_path="ask",
                )
                await store.set_merge_candidate_status(c["id"], "rejected")
                deferred += 1
        except Exception:
            _log.warning("reconcile failed for candidate %s; skipping", c.get("id"), exc_info=True)
    if len(candidates) > _MAX_RECONCILE:
        _log.info("reconcile capped: %d of %d candidates", _MAX_RECONCILE, len(candidates))
    return {"merged": merged, "rejected": rejected, "deferred": deferred}


def _scalarish(value: str) -> bool:
    """Cheaply skip values that are obviously genuine scalars (dates, numbers,
    emails, long descriptions) — only proper-noun-ish strings are worth asking
    the model about. Capitalization-agnostic."""
    v = value.strip()
    return not v or len(v) > 40 or "@" in v or any(ch.isdigit() for ch in v)


def _json_array(text: str) -> list:
    """Parse the first JSON array out of the model's reply (tolerant of stray prose)."""
    data = json_between(text, "[", "]")
    return data if isinstance(data, list) else []


async def _judge_indices(brain, system: str, prompt: str, n: int, *, max_tokens: int = 200):
    """Run one LLM 'reply with a JSON array of line numbers' judgement and return only the
    valid, in-range indices (0 ≤ i < n). On any failure → []. The shared core of the dream's
    line-number passes, so they all parse and bounds-check identically (the easiest place for
    them to drift was each re-implementing this)."""
    try:
        resp = await brain.llm.complete(system, [Msg("user", prompt)], max_tokens=max_tokens)
    except Exception:
        _log.warning("dream judgement failed", exc_info=True)
        return []
    return [i for i in _json_array(resp.text) if isinstance(i, int) and 0 <= i < n]


async def _reclassify(brain) -> int:
    """Fix attributes that misfiled a named subject as a plain string — using the
    model's world knowledge, not brittle rules. It hands over the suspect facts
    and asks which VALUES are really subjects (and their kind/relationship), then
    converts them to subject + edge and supersedes the string. Capitalization- and
    language-agnostic; bounded per night."""
    store = brain.store
    suspect = await _rotate(
        store, "dream_cursor_reclassify",
        [a for a in await store.all_attributes() if not _scalarish(a.value)], _NIGHTLY_LLM_BATCH,
    )
    if not suspect:
        return 0
    lines = []
    for i, a in enumerate(suspect):
        ent = await store.get_subject(a.subject_id)
        lines.append(f"{i}: {(ent.name if ent else '?')} | {a.key} | {a.value}")
    prompt = (
        "Some stored facts may have put a named real-world subject (a place, person, "
        "organization, etc.) into a plain string value, when it should be its own subject "
        "with a relationship. For each line whose VALUE is such a named subject, return it; "
        "skip lines whose value is a genuine scalar (a date, number, title, or description). "
        'Reply with ONLY a JSON array of {"i": <line number>, "kind": '
        '"place|person|org|topic|...", "relation": "<short relationship from subject to value, '
        'e.g. located_in/has_capital/works_at>"}.\n\n' + "\n".join(lines)
    )
    try:
        resp = await brain.llm.complete(
            "You tidy a personal memory graph using your world knowledge.",
            [Msg("user", prompt)],
            max_tokens=500,
        )
    except Exception:
        _log.warning("reclassify call failed", exc_info=True)
        return 0

    source = await store.add_source("inference", ref="dream:reclassify")
    converted = 0
    for item in _json_array(resp.text):
        idx = item.get("i") if isinstance(item, dict) else None
        if not isinstance(idx, int) or not (0 <= idx < len(suspect)):
            continue
        attr = suspect[idx]
        kind = str(item.get("kind") or "topic")
        rel = str(item.get("relation") or "related_to")
        made = False
        # Match brain._split_places' strip()+drop-empty behaviour (kept inline to
        # avoid a heavy/circular import from brain.py) so a compound value doesn't
        # shatter into blank or whitespace-padded subjects.
        for part in (p.strip() for p in re.split(r",| and |&|/", str(attr.value)) if p.strip()):
            found = await store.find_by_name(part)
            vid = found.id if found else (await store.add_subject(kind, part)).id
            if vid != attr.subject_id:
                await store.add_relationship(
                    attr.subject_id, vid, rel, source_id=source.id, confidence=0.7
                )
                made = True
        if made:
            await store.end_attribute(attr.id)  # the string is superseded, not deleted
            converted += 1
    return converted


async def _review_value_changes(brain) -> int:
    """Judge the day's value changes with world knowledge: which are genuine
    contradictions (worth a verification gap) vs. mere refinements/corrections.
    Replaces the old write-time substring heuristic."""
    store = brain.store
    since = (now_utc() - dt.timedelta(hours=25)).isoformat()
    changes = (await store.recent_attribute_changes(since))[:_NIGHTLY_LLM_BATCH]
    if not changes:
        return 0
    lines = [
        f"{i}: {c['name']}'s {c['key']}: \"{c['old_value']}\" → \"{c['new_value']}\""
        for i, c in enumerate(changes)
    ]
    prompt = (
        "Some stored facts changed value. For each, decide if the change is a genuine "
        "CONTRADICTION worth double-checking (two incompatible claims) or just a refinement / "
        "correction (more specific, a fuller name, a typo fix). Reply with ONLY a JSON array of "
        "the line numbers that are genuine contradictions.\n\n" + "\n".join(lines)
    )
    existing = [g.description for g in await store.open_gaps(limit=200)]
    opened = 0
    for idx in await _judge_indices(brain, "You review changes in a memory graph.", prompt,
                                    len(changes)):
        c = changes[idx]
        marker = (
            f"verify: {c['name']}'s {c['key'].replace('_', ' ')} changed to "
            f"{c['new_value']} (was {c['old_value']})"
        )
        if marker not in existing:
            await store.add_gap(
                marker, "verification", resolution_path="ask", about_id=c["subject_id"]
            )
            existing.append(marker)
            opened += 1
    return opened


async def _dedup_gaps(brain) -> int:
    """Semantically merge open curiosity gaps that are the same question worded
    differently — the dream's job (the write-time check is only cheap lexical)."""
    store = brain.store
    gaps = await store.open_gaps(limit=50)
    if len(gaps) < 2:
        return 0
    lines = [f"{i}: {g.description}" for i, g in enumerate(gaps)]
    prompt = (
        "These are open questions/notes to follow up on. Some may be duplicates — the same thing "
        "worded differently. Reply with ONLY a JSON array of the line numbers that are redundant "
        "duplicates of an EARLIER line (keep the first of each distinct item, drop the rest).\n\n"
        + "\n".join(lines)
    )
    dropped = 0
    src = None
    for idx in await _judge_indices(brain, "You tidy a list of open questions.", prompt, len(gaps)):
        # Attribute the automated closure to a dream source, so an LLM-judged abandon is
        # auditable (and a wrong one traceable), not anonymous.
        src = src or await store.add_source("inference", ref="dream:dedup_gaps")
        await store.set_gap_status(gaps[idx].id, "abandoned", resolved_by=src.id)
        dropped += 1
    return dropped


async def _resolve_answered_gaps(brain) -> int:
    """Close open gaps the graph now answers — the fuzzy / no-`about_id` ones the
    write-time structural pass (`resolve_satisfied_gaps`) can't catch, e.g. "what
    was in the package?" once HeroQuest is recorded. Conservative: only the
    clearly-answered ones, so Moko never stays curious about what it already
    knows, yet a genuinely open question is left open."""
    store = brain.store
    gaps = await store.open_gaps(limit=50)
    if not gaps:
        return 0
    subjects = await _rotate(
        store, "dream_cursor_gap_facts", await store.all_subjects(), _NIGHTLY_LLM_BATCH
    )
    facts = []
    for e in subjects:
        attrs = await store.attributes_for(e.id)
        detail = "; ".join(f"{a.key}={a.value}" for a in attrs[:8])
        facts.append(f"- {e.name} ({e.kind}){f': {detail}' if detail else ''}")
    gap_lines = [f"{i}: {g.description}" for i, g in enumerate(gaps)]
    prompt = (
        "Here is what's known in a personal memory graph:\n" + "\n".join(facts)
        + "\n\nAnd here are open questions still being tracked:\n" + "\n".join(gap_lines)
        + "\n\nReply with ONLY a JSON array of the line numbers of questions the known facts "
        "above now ANSWER, so they can be closed. Be strict: include a number only if the "
        "answer is clearly present. If unsure, leave it out."
    )
    resolved = 0
    src = None
    for idx in await _judge_indices(
        brain, "You close out answered questions in a memory graph. Be conservative.",
        prompt, len(gaps),
    ):
        # Auditable provenance for the automated close (the schema's resolved_by is exactly
        # for this), so a hallucinated closure is traceable.
        src = src or await store.add_source("inference", ref="dream:resolve_gaps")
        await store.set_gap_status(gaps[idx].id, "resolved", resolved_by=src.id)
        resolved += 1
    return resolved


async def _relationship_sanity(brain) -> int:
    """Retire relationships the model judges redundant, backwards, or nonsensical
    — a 'currently in X' edge that merely duplicates 'lives in X', or a country
    'located_in' a city. Conservative: anything that could be separately true is
    kept. Bounded per night."""
    store = brain.store
    rels = await _rotate(
        store, "dream_cursor_rel_sanity", await store.all_relationships(), _NIGHTLY_LLM_BATCH
    )
    if len(rels) < 2:
        return 0
    names = {e.id: (e.name, e.kind) for e in await store.all_subjects()}
    lines = []
    for i, r in enumerate(rels):
        a_n, a_k = names.get(r.from_id, ("?", "?"))
        b_n, b_k = names.get(r.to_id, ("?", "?"))
        lines.append(f"{i}: {a_n} ({a_k}) --{r.type}--> {b_n} ({b_k})")
    prompt = (
        "These are directed relationships in a personal memory graph. Flag only the ones to "
        "RETIRE because they are:\n"
        "- redundant: a weaker edge fully implied by a stronger one between the SAME pair "
        "(e.g. 'located_in Sydney' when 'lives_in Sydney' is also present);\n"
        "- backwards or nonsensical (e.g. a country located_in a city; a place located_in a "
        "language).\n"
        "Keep everything that is genuinely, separately true — a person can both live in one city "
        "and be visiting another, and a place can sit inside several larger places. Reply with "
        "ONLY a JSON array of the line numbers to retire.\n\n" + "\n".join(lines)
    )
    retired = 0
    for idx in await _judge_indices(
        brain, "You tidy a memory graph's relationships. Be conservative — keep anything that "
        "could be separately true.", prompt, len(rels),
    ):
        await store.end_relationship(rels[idx].id)  # superseded, kept in history
        retired += 1
    return retired


async def _wonder(brain) -> str:
    """A short private reflection for the journal — what Moko finds itself
    wondering about after consolidating. Best-effort; empty on failure."""
    gaps = await brain.store.open_gaps(limit=5)
    if not gaps:
        return ""
    threads = "; ".join(g.description for g in gaps)
    ask = f"In one short sentence, what do you wonder about? Open threads: {threads}"
    try:
        resp = await brain.llm.complete(
            "You are Moko, reflecting privately at night after tidying your memory.",
            [Msg("user", ask)],
            max_tokens=60,
        )
        return resp.text.strip()
    except Exception:
        _log.warning("dream wonder failed", exc_info=True)
        return ""


async def dream(brain, now: dt.datetime, *, should_yield=None) -> dict:
    """Run one consolidation pass. `now` (local) stamps the dreams start, so
    scheduling stays consistent with the same clock `should_dream` reads.
    Returns its stats (also stored in dreams).

    `should_yield`, when given, is polled between consolidation passes: if it
    returns True the dream finishes early (a summary noting it was cut short +
    whatever partial stats accumulated) and returns. The gateway runs the dream
    inline on the single serial consumer, so this lets an inbound message
    pre-empt a long night instead of waiting minutes. Cutting short is safe: the
    first-of-night decay guard (the `decays_applied` app_state key) and the
    `_rotate` cursors make a partial night resume correctly next interval. Default
    (`None`) never yields — today's exact behaviour, so existing callers are
    unaffected."""
    store = brain.store
    usage_kind.set("dream")  # label every brain-model call this pass makes
    started_at = brain.clock.to_utc(now).isoformat()
    await store.close_orphaned_dreams(before=started_at)  # tidy any hard-kill leftover
    # Bound the LLM-I/O ledger: clear verbatim prompts older than the retention window (the
    # token counts stay for the cost history). Best-effort housekeeping.
    try:
        cutoff = (now_utc() - dt.timedelta(days=LLM_IO_RETENTION_DAYS)).isoformat()
        await store.usage.prune_llm_call_io(cutoff)
    except Exception:
        _log.warning("llm-usage I/O prune failed", exc_info=True)
    # The once-a-night decays (thought fade, gap salience) must fire only on the FIRST
    # dream of the sleep window — Moko now dreams several times a night, and re-applying
    # them each time would over-fade. Guard on a single `decays_applied` key in app_state
    # whose VALUE is the window's opening instant (not a date-stamped key), set only after
    # the decays apply — so the invariant holds regardless of dream-row ordering or
    # orphan-cleanup timing, and a crash *before* the decays simply lets the next dream
    # apply them (rather than skipping the night).
    gov = ProactivityGovernor(brain.config)
    opened_utc = brain.clock.to_utc(gov.quiet_opened_at(now)).isoformat()
    first_tonight = await store.get_state("decays_applied") != opened_utc
    dream_id = await store.start_dream(at=started_at)
    _log.info("dream: begin%s", "" if first_tonight else " (later pass tonight)")

    # Partial-night-safe: every stat is initialised so a mid-dream yield still writes a
    # well-formed record, and the passes overwrite them in order as they complete.
    thoughts_faded = gaps_rescored = 0
    reclassified = contradictions = deduped = gaps_answered = 0
    gaps_opened = rels_tidied = search_healed = 0
    reconciled = {"merged": 0, "rejected": 0, "deferred": 0}
    wonders = ""

    def _wants_yield() -> bool:
        return should_yield is not None and should_yield()

    def _stats() -> dict:
        return {
            "thoughts_faded": thoughts_faded, "gaps_rescored": gaps_rescored,
            "reclassified": reclassified, "contradictions": contradictions,
            "gaps_deduped": deduped, "gaps_answered": gaps_answered,
            "gaps_opened": gaps_opened, "rels_tidied": rels_tidied,
            "search_healed": search_healed, **reconciled,
        }

    async def _finish_early() -> dict:
        stats = _stats()
        await store.finish_dream(
            dream_id, summary="cut short to answer the companion; " + str(stats),
            stats=stats, wonders=wonders,
        )
        _log.info("dream: yielded early %s", stats)
        return stats

    try:
        await store.end_session("dream")  # wake with fresh working memory
        if first_tonight:  # the nightly decays — once per sleep window only
            thoughts_faded = await store.decay_thoughts(rate=_PRUDENCE_RATE, floor=_PRUDENCE_FLOOR)
            gaps_rescored = await store.decay_gap_salience(factor=_GAP_DECAY, floor=_GAP_FLOOR)
            await store.set_state("decays_applied", opened_utc)  # mark this window's decays done
        # Between-pass yield points: if an inbound message is waiting, stop after the current
        # pass and finish early. A `_rotate` cursor already advanced, so the next interval
        # resumes where this left off — no pass is re-run wholesale or skipped for the night.
        if _wants_yield():
            return await _finish_early()
        reconciled = await _reconcile(brain)
        if _wants_yield():
            return await _finish_early()
        reclassified = await _reclassify(brain)  # misfiled subject-values → edges
        if _wants_yield():
            return await _finish_early()
        contradictions = await _review_value_changes(brain)  # judge changes (LLM)
        if _wants_yield():
            return await _finish_early()
        deduped = await _dedup_gaps(brain)  # merge same-question gaps (LLM)
        if _wants_yield():
            return await _finish_early()
        gaps_answered = await _resolve_answered_gaps(brain)  # close what's now known (LLM)
        if _wants_yield():
            return await _finish_early()
        rels_tidied = await _relationship_sanity(brain)  # retire redundant/bad edges (LLM)
        if _wants_yield():
            return await _finish_early()
        # Deepen curiosity: a whole-graph sweep for what Moko knows OF but little ABOUT —
        # people/pets without facts, unsure relationships, facts gone stale — opening gaps
        # to pursue later. This is the periodic sweep the detector was always meant for
        # (the per-turn path only sees what a turn touched); the dream is where Moko, like a
        # person turning the day over, gets curious about the thinly-known. Idempotent
        # (dedups against open gaps), so it's safe to run each night.
        companion_id, _ = await brain._ensure_actors()
        gaps_opened = len(await detect_gaps(store, companion_id=companion_id))
        await store.optimize_search()  # compact the recall index (safe upkeep)
        # Drop stale FTS rows left behind by superseded attributes / merged-away
        # subjects, so recall never surfaces a dead node's text (safe upkeep).
        search_healed = await store.heal_search()
        wonders = await _wonder(brain)
    except Exception:
        # Mark the run ended (crashed) so it isn't retried every tick tonight —
        # which would re-run the once-a-night decays. It resumes next window.
        _log.warning("dream crashed mid-run; marking ended", exc_info=True)
        await store.finish_dream(dream_id, summary="crashed mid-run")
        raise

    stats = _stats()
    summary = (
        f"{thoughts_faded} thought(s) faded, {gaps_rescored} re-scored, "
        f"{reconciled['merged']} merged, {reclassified} reclassified, "
        f"{contradictions} to verify, {deduped} duplicate gap(s) merged, "
        f"{gaps_answered} answered gap(s) closed, {rels_tidied} edge(s) tidied."
    )
    await store.finish_dream(dream_id, summary=summary, stats=stats, wonders=wonders)
    _log.info("dream: done %s", stats)
    return stats
