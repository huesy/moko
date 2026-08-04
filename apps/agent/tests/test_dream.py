"""The nightly dream — scheduling (once per night, in-window) and reflection
(decay never deletes; the run is journaled)."""

import datetime as dt

import pytest

from moko.agent import Brain, dream, should_dream
from moko.agent.proactivity import window_opened_at
from moko.config.config import Config
from moko.llm.base import LLMResponse
from moko.store import Store

CFG = Config(
    companion_name="Andrew",
    timezone="Asia/Tokyo",
    sleep_start="23:00",
    sleep_end="07:00",
    proactivity="moderate",
)


class FakeLLM:
    """`verdict` controls reconciliation (SAME/DIFFERENT/UNSURE); other completes
    return a bland line."""

    model = "fake"

    def __init__(self, verdict="UNSURE"):
        self.verdict = verdict

    async def complete(self, system, messages, **kw):
        if "SAME, DIFFERENT, or UNSURE" in messages[-1].content:
            return LLMResponse(self.verdict)
        return LLMResponse("I wonder how their week is going.")


async def _candidate(store, name="Mercury"):
    """Create two same-name, different-kind subjects (auto-flagged as a merge
    candidate) and return the candidate row (id, a_id→folded, b_id→canonical)."""
    await store.add_subject("person", name)
    await store.add_subject("place", name)
    cur = await store.conn.execute(
        "SELECT id, a_id, b_id FROM merge_candidates WHERE status='open'"
    )
    return await cur.fetchone()


def _at(hour: int) -> dt.datetime:
    return dt.datetime(2026, 6, 24, hour, 0, tzinfo=dt.UTC)


@pytest.fixture
async def store(tmp_path):
    s = await Store.open(tmp_path / "moko.db")
    # Store.heal_search() is being added concurrently by another agent; until it lands,
    # stub a no-op so the dream's real call resolves. Once the real method exists this
    # fixture leaves it untouched (hasattr guard), and the full suite exercises the real one.
    if not hasattr(s, "heal_search"):
        async def _heal_search() -> int:
            return 0

        s.heal_search = _heal_search
    try:
        yield s
    finally:
        await s.close()


# ── scheduling ─────────────────────────────────────────────────────────
def test_window_opened_at_same_day_and_wrap():
    # at 02:00, a 23:00 window opened *yesterday*; at 23:30, it opened today.
    assert window_opened_at(_at(2), "23:00").day == 23
    assert window_opened_at(dt.datetime(2026, 6, 24, 23, 30, tzinfo=dt.UTC), "23:00").day == 24


async def test_should_dream_only_in_window_and_once(store):
    brain = Brain(store, FakeLLM(), CFG)
    assert not await should_dream(brain, _at(12))  # daytime → no
    assert await should_dream(brain, _at(2))  # in window, not yet dreamt → yes

    await dream(brain, _at(2))  # dream completes
    assert not await should_dream(brain, _at(2))  # already dreamt tonight → no


async def test_dream_repeats_within_the_sleep_window(store, monkeypatch):
    # Moko now dreams SEVERAL times a night, spaced by the interval — but the once-a-night
    # decays fire only on the first dream of the window.
    brain = Brain(store, FakeLLM(), CFG)
    decays = {"n": 0}
    orig = store.decay_thoughts

    async def counting(*a, **k):
        decays["n"] += 1
        return await orig(*a, **k)

    monkeypatch.setattr(store, "decay_thoughts", counting)

    assert await should_dream(brain, _at(2))  # first dream of the night
    await dream(brain, _at(2))
    assert not await should_dream(brain, _at(2))  # not again immediately (interval)

    later = _at(2) + dt.timedelta(minutes=91)  # past the 90-min interval, still before wake
    assert await should_dream(brain, later)  # dreams again
    await dream(brain, later)

    assert decays["n"] == 1  # the nightly decay ran only on the first dream
    assert len(await store.recent_dreams(limit=5)) == 2  # two dream events this window


async def test_dream_opens_curiosity_about_thinly_known_people(store):
    # The dream runs the whole-graph curiosity sweep: someone in the companion's circle
    # that Moko knows OF but holds no facts about → a gap to learn who they are. (Before,
    # this sweep was dead code, wired nowhere — reflection never deepened curiosity.)
    brain = Brain(store, FakeLLM(), CFG)
    companion_id, _ = await brain._ensure_actors()
    wren = await store.add_subject("person", "Wren")  # named, but no facts
    src = await store.add_source("user_stated")
    await store.add_relationship(companion_id, wren.id, "friend_of", source_id=src.id)

    stats = await dream(brain, _at(2))
    assert stats["gaps_opened"] >= 1
    gaps = [g.description for g in await store.open_gaps(limit=20)]
    assert any("Wren" in d for d in gaps)  # Moko got curious about the thinly-known friend


async def test_hard_killed_dream_does_not_rerun_tonight(store):
    # A SIGKILL/OOM/power-loss mid-dream leaves a started run with ended_at NULL.
    # should_dream must treat tonight as attempted so the next tick doesn't re-run
    # the whole dream and double-apply the once-a-night prudence/gap decays.
    brain = Brain(store, FakeLLM(), CFG)
    assert await should_dream(brain, _at(2))  # eligible before any dream
    await store.start_dream(at=brain.clock.to_utc(_at(2)).isoformat())  # never finished
    assert not await should_dream(brain, _at(2))  # attempted → won't re-run
    dreams = await store.recent_dreams(limit=5)
    assert len(dreams) == 1 and dreams[0]["ended_at"] is None  # accurately still in-progress


async def test_dream_rotate_pages_the_whole_graph_across_nights(store):
    # The cost-capped cleanup passes must eventually cover the whole graph, not
    # re-review the same first 30 rows forever.
    from moko.agent.dream import _rotate

    items = list(range(70))
    assert await _rotate(store, "k", items, 30) == list(range(0, 30))
    assert await _rotate(store, "k", items, 30) == list(range(30, 60))
    assert await _rotate(store, "k", items, 30) == list(range(60, 70))  # tail
    assert await _rotate(store, "k", items, 30) == list(range(0, 30))  # wrapped to start
    # a list that fits in one night isn't paged (no cursor churn)
    assert await _rotate(store, "k2", list(range(10)), 30) == list(range(10))


async def test_next_dream_tidies_an_orphaned_run(store):
    brain = Brain(store, FakeLLM(), CFG)
    await store.start_dream(at=brain.clock.to_utc(_at(2)).isoformat())  # hard-kill leftover
    await dream(brain, _at(2) + dt.timedelta(days=1))  # next night runs and tidies it
    dreams = await store.recent_dreams(limit=5)
    assert all(d["ended_at"] for d in dreams)  # nothing left perpetually "in progress"
    assert any("interrupted" in (d["summary"] or "") for d in dreams)


async def test_recent_dreams_and_outreach_for_journal(store):
    await dream(Brain(store, FakeLLM(), CFG), _at(2))
    await store.log_outreach("telegram", "curiosity", counts_to_budget=True)

    dreams = await store.recent_dreams(limit=5)
    assert len(dreams) == 1 and dreams[0]["ended_at"] and dreams[0]["summary"]
    out = await store.recent_outreach(limit=5)
    assert len(out) == 1 and out[0]["kind"] == "curiosity"


async def test_should_not_dream_during_active_chat(store):
    brain = Brain(store, FakeLLM(), CFG)
    session = await store.ensure_session()
    await store.add_message("cli", "in", "still up!", session_id=session.id)
    # In the window, never dreamt — but a fresh message means don't end the session.
    assert not await should_dream(brain, _at(2))


async def test_dream_crash_is_marked_ended_not_retried(store, monkeypatch):
    brain = Brain(store, FakeLLM(), CFG)

    async def boom(**kw):
        raise RuntimeError("boom")

    monkeypatch.setattr(store, "decay_thoughts", boom)
    with pytest.raises(RuntimeError):
        await dream(brain, _at(2))

    cur = await store.conn.execute("SELECT ended_at, summary FROM dreams")
    row = await cur.fetchone()
    assert row["ended_at"] and row["summary"] == "crashed mid-run"  # marked, not orphaned
    assert not await should_dream(brain, _at(2))  # so it won't re-run every tick


# ── reflection (decay never deletes) ───────────────────────────────────
async def test_dream_decays_thoughts_without_deleting(store):
    brain = Brain(store, FakeLLM(), CFG)
    await store.add_thought("note", "a passing impression")
    await dream(brain, _at(2))

    cur = await store.conn.execute("SELECT prudence FROM thoughts")
    rows = await cur.fetchall()
    assert len(rows) == 1  # still there — nothing deleted
    assert 0.05 <= rows[0]["prudence"] < 1.0  # faded toward the floor, not gone


async def test_dream_rescores_gaps_and_journals(store):
    brain = Brain(store, FakeLLM(), CFG)
    gap = await store.add_gap("identity of Sam (person)", "dangling_ref", salience=0.8)
    stats = await dream(brain, _at(2))

    assert (await store.get_gap(gap.id)).salience < 0.8  # salience decayed
    assert stats["gaps_rescored"] >= 1
    # the run is journaled with start + end + stats + a private wondering
    cur = await store.conn.execute("SELECT started_at, ended_at, stats, wonders FROM dreams")
    row = await cur.fetchone()
    assert row["started_at"] and row["ended_at"] and "gaps_rescored" in row["stats"]
    assert row["wonders"]  # a reflection was written (open gaps existed)


async def test_dream_reclassifies_misfiled_subject_value(store):
    # A named place misfiled under an ambiguous key ("region") as a string —
    # the dream uses the model's world knowledge to convert it to a subject+edge.
    src = await store.add_source("user_stated")
    melb = await store.add_subject("place", "Melbourne")
    await store.set_attribute(melb.id, "region", "Victoria", source_id=src.id)

    class ReclassLLM:
        model = "fake"

        async def complete(self, system, messages, **kw):
            if "JSON array" in messages[-1].content:
                return LLMResponse('[{"i": 0, "kind": "place", "relation": "located_in"}]')
            return LLMResponse("ok")

    await dream(Brain(store, ReclassLLM(), CFG), _at(2))

    victoria = await store.find_by_name("Victoria")
    assert victoria is not None and victoria.kind == "place"  # now its own subject
    assert any(r.type == "located_in" for r in await store.relationships_for(melb.id))
    assert all(a.key != "region" for a in await store.attributes_for(melb.id))  # string superseded


async def test_dream_flags_real_contradictions_in_value_changes(store):
    # A value change (Lisbon → Porto) is judged a contradiction by the LLM → a
    # verification gap. This judgment moved out of write-time string checks.
    src = await store.add_source("user_stated")
    maya = await store.add_subject("person", "Maya")
    await store.set_attribute(maya.id, "city", "Lisbon", source_id=src.id)
    await store.set_attribute(maya.id, "city", "Porto", source_id=src.id)  # supersedes

    class ChangeLLM:
        model = "fake"

        async def complete(self, system, messages, **kw):
            if "contradiction" in messages[-1].content.lower():
                return LLMResponse("[0]")  # judged a real contradiction
            return LLMResponse("[]")

    await dream(Brain(store, ChangeLLM(), CFG), _at(2))
    gaps = [g.description.lower() for g in await store.open_gaps(limit=50)]
    assert any("verify" in g and "city" in g for g in gaps)


async def test_dream_retires_redundant_relationship(store):
    # Maya lives_in Sydney AND located_in Sydney → the located_in is redundant
    # once she lives there; the dream's LLM retires it (kept in history).
    src = await store.add_source("user_stated")
    maya = await store.add_subject("person", "Maya")
    sydney = await store.add_subject("place", "Sydney")
    await store.add_relationship(maya.id, sydney.id, "lives_in", source_id=src.id)
    await store.add_relationship(maya.id, sydney.id, "located_in", source_id=src.id)

    class SanityLLM:
        model = "fake"

        async def complete(self, system, messages, **kw):
            text = messages[-1].content
            if "RETIRE" in text:  # the relationship-sanity prompt
                for line in text.splitlines():
                    if "located_in" in line and line[:1].isdigit():
                        return LLMResponse(f"[{line.split(':')[0]}]")
            return LLMResponse("[]")

    await dream(Brain(store, SanityLLM(), CFG), _at(2))
    current = {r.type for r in await store.relationships_for(maya.id)}
    assert "lives_in" in current and "located_in" not in current  # redundant edge retired


async def test_dream_dedups_similar_gaps(store):
    # Two rephrasings of the same question → the LLM merges them (one abandoned).
    await store.add_gap("What's the testing server's name?", "missing_attr", resolution_path="ask")
    await store.add_gap("Name of the testing server?", "missing_attr", resolution_path="ask")

    class DedupLLM:
        model = "fake"

        async def complete(self, system, messages, **kw):
            if "duplicates" in messages[-1].content.lower():
                return LLMResponse("[1]")  # drop the second as a duplicate
            return LLMResponse("[]")

    await dream(Brain(store, DedupLLM(), CFG), _at(2))
    assert len(await store.open_gaps(limit=50)) == 1  # one merged away


async def test_dream_closes_answered_gaps(store):
    # A no-`about_id` gap the graph now answers gets closed by the dream's LLM
    # pass (the structural write-time pass can't reach it).
    src = await store.add_source("user_stated")
    hq = await store.add_subject("object", "HeroQuest")
    await store.set_attribute(hq.id, "category", "board game", source_id=src.id)
    await store.add_gap(
        "What was in the package Andrew received?", "missing_attr", resolution_path="ask"
    )

    class ResolveLLM:
        model = "fake"

        async def complete(self, system, messages, **kw):
            if "now ANSWER" in messages[-1].content:
                return LLMResponse("[0]")  # the package question is answered
            return LLMResponse("[]")

    stats = await dream(Brain(store, ResolveLLM(), CFG), _at(2))
    assert stats["gaps_answered"] == 1
    assert not any("package" in g.description for g in await store.open_gaps(limit=50))


async def test_dream_ends_the_active_session(store):
    brain = Brain(store, FakeLLM(), CFG)
    await store.ensure_session()
    await dream(brain, _at(2))
    assert await store.current_session() is None  # fresh working memory on wake


# ── reconciliation (LLM-judged, non-destructive) ───────────────────────
async def test_reconcile_merges_when_same(store):
    src = await store.add_source("user_stated")
    cand = await _candidate(store)
    # A fact on the subject that gets folded in (a_id merges INTO b_id).
    await store.set_attribute(cand["a_id"], "note", "carry me over", source_id=src.id)

    stats = await dream(Brain(store, FakeLLM("SAME"), CFG), _at(2))
    assert stats["merged"] == 1
    # The fact moved to the canonical subject (not duplicated), with an audit row.
    assert any(at.key == "note" for at in await store.attributes_for(cand["b_id"]))
    assert await store.attributes_for(cand["a_id"]) == []  # moved, not copied
    cur = await store.conn.execute("SELECT COUNT(*) AS c FROM subject_merges")
    assert (await cur.fetchone())["c"] == 1  # auditable, reversible
    # The folded subject is tagged merged and no longer findable — so new facts
    # can't re-attach to a dead node, and recall won't surface it.
    cur = await store.conn.execute(
        "SELECT merged_into FROM subjects WHERE id = ?", (cand["a_id"],)
    )
    assert (await cur.fetchone())["merged_into"] == cand["b_id"]
    assert await store.find_by_name("Mercury") is not None  # canonical still found
    cur = await store.conn.execute(
        "SELECT COUNT(*) AS c FROM search WHERE ref_id = ?", (cand["a_id"],)
    )
    assert (await cur.fetchone())["c"] == 0  # stale FTS rows for the dead node gone


async def test_merge_creates_no_self_loops(store):
    # If the two flagged subjects have a relationship between them, the merge
    # must not fold it into a meaningless A->A self-loop.
    src = await store.add_source("user_stated")
    cand = await _candidate(store)
    await store.add_relationship(cand["a_id"], cand["b_id"], "related_to", source_id=src.id)

    await dream(Brain(store, FakeLLM("SAME"), CFG), _at(2))
    cur = await store.conn.execute(
        "SELECT COUNT(*) AS c FROM relationships WHERE from_id = to_id AND valid_to IS NULL"
    )
    assert (await cur.fetchone())["c"] == 0  # no current self-loop edges


async def test_reconcile_rejects_when_different(store):
    await _candidate(store)
    stats = await dream(Brain(store, FakeLLM("DIFFERENT"), CFG), _at(2))
    assert stats["rejected"] == 1 and stats["merged"] == 0
    cur = await store.conn.execute("SELECT COUNT(*) AS c FROM subject_merges")
    assert (await cur.fetchone())["c"] == 0  # nothing merged


async def test_reconcile_defers_to_companion_when_unsure(store):
    await _candidate(store)
    stats = await dream(Brain(store, FakeLLM("UNSURE"), CFG), _at(2))
    assert stats["deferred"] == 1
    gaps = [g.description.lower() for g in await store.open_gaps(limit=50)]
    assert any("same as" in g for g in gaps)  # a disambiguation question was opened


async def test_reconcile_judge_sees_each_subjects_facts_not_just_name(store):
    # The judge must decide on real evidence (facts + edges), not bare name+kind —
    # that's exactly the ambiguous case (same name, different kind).
    from moko.agent.dream import _judge_same

    a = await store.add_subject("person", "Mercury")
    b = await store.add_subject("place", "Mercury")
    src = await store.add_source("user_stated")
    await store.set_attribute(a.id, "role", "courier", source_id=src.id)
    await store.set_attribute(b.id, "country", "Nevada", source_id=src.id)
    candidate = {
        "a_id": a.id, "b_id": b.id, "a_name": "Mercury", "a_kind": "person",
        "b_name": "Mercury", "b_kind": "place", "evidence": "same name",
    }
    seen = {}

    class CaptureLLM:
        model = "fake"

        async def complete(self, system, messages, **kw):
            seen["prompt"] = messages[-1].content
            return LLMResponse("DIFFERENT")

    verdict = await _judge_same(Brain(store, CaptureLLM(), CFG), candidate)
    assert verdict == "DIFFERENT"
    assert "courier" in seen["prompt"] and "Nevada" in seen["prompt"]  # facts reached the judge


# ── FTS self-heal + cooperative yield ──────────────────────────────────
async def test_dream_heals_stale_search_rows(store, monkeypatch):
    # The dream calls store.heal_search() alongside optimize_search() to drop stale
    # FTS rows (superseded attributes / merged-away subjects), and folds the count
    # into its stats. Spy on the call (the real method lands from a concurrent agent).
    calls = {"n": 0}

    async def spy_heal() -> int:
        calls["n"] += 1
        return 3  # pretend three stale rows were reaped

    monkeypatch.setattr(store, "heal_search", spy_heal, raising=False)
    stats = await dream(Brain(store, FakeLLM(), CFG), _at(2))
    assert calls["n"] == 1  # heal ran exactly once this dream
    assert stats["search_healed"] == 3  # and its count reached the journal's stats
    cur = await store.conn.execute("SELECT stats FROM dreams")
    assert '"search_healed": 3' in (await cur.fetchone())["stats"]  # persisted, not just returned


async def test_dream_yields_early_when_asked(store):
    # The gateway runs the dream inline on the serial consumer; a should_yield hook lets an
    # inbound message pre-empt it. Yielding after the first pass still writes a dream record,
    # with the decays applied but the later consolidation passes skipped this interval.
    brain = Brain(store, FakeLLM(), CFG)
    await store.add_thought("note", "a passing impression")
    reconciles = {"n": 0}
    orig = store.open_merge_candidates

    async def counting(*a, **k):
        reconciles["n"] += 1
        return await orig(*a, **k)

    store.open_merge_candidates = counting

    stats = await dream(brain, _at(2), should_yield=lambda: True)  # yield at the first check

    # A dream record was still written, marked cut short, with the partial (decay-only) stats.
    dreams = await store.recent_dreams(limit=5)
    assert len(dreams) == 1 and dreams[0]["ended_at"]
    assert "cut short" in dreams[0]["summary"]
    assert stats["thoughts_faded"] == 1  # the first-of-night decays did run before the yield
    assert reconciles["n"] == 0  # but the reconcile pass (and everything after) was skipped
    assert stats["merged"] == 0 and stats["gaps_answered"] == 0


async def test_dream_default_never_yields(store):
    # should_yield defaults to None → today's exact behaviour, all passes run to completion.
    brain = Brain(store, FakeLLM(), CFG)
    stats = await dream(brain, _at(2))  # no should_yield
    dreams = await store.recent_dreams(limit=5)
    assert len(dreams) == 1 and "cut short" not in (dreams[0]["summary"] or "")
    assert "search_healed" in stats  # the full run reached the upkeep passes


async def test_dream_split_strips_and_drops_empty_parts(store):
    # dream._reclassify splits a compound value like brain._split_places does: strip each
    # part and drop empties, so a value with padded / trailing separators doesn't spawn
    # blank or whitespace subjects.
    src = await store.add_source("user_stated")
    melb = await store.add_subject("place", "Melbourne")
    # Padded parts and a trailing separator that a naive split would turn into "" / "  ".
    await store.set_attribute(melb.id, "region", " Victoria , Australia /", source_id=src.id)

    class ReclassLLM:
        model = "fake"

        async def complete(self, system, messages, **kw):
            if "JSON array" in messages[-1].content:
                return LLMResponse('[{"i": 0, "kind": "place", "relation": "located_in"}]')
            return LLMResponse("ok")

    await dream(Brain(store, ReclassLLM(), CFG), _at(2))

    names = {e.name for e in await store.all_subjects()}
    assert "Victoria" in names and "Australia" in names  # both real parts, stripped clean
    assert "" not in names and not any(n != n.strip() for n in names)  # no blank/padded junk
