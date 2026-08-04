"""The curiosity gap-detection engine (M4 Phase A)."""

import datetime as dt

import pytest

from moko.agent import detect_gaps, detect_gaps_for
from moko.agent.curiosity import (
    _score,
    stale_facts,
    thin_subjects,
    unconfirmed_relationships,
)
from moko.store import Store
from moko.store.models import Attribute, Relationship, Subject

NOW = dt.datetime(2026, 6, 23, tzinfo=dt.UTC)


def _ent(eid, kind, name):
    return Subject(eid, kind, name, 1.0, "", NOW.isoformat())


def _attr(eid, key, value, *, valid_from=None):
    return Attribute("a", eid, key, value, "string", valid_from, None, "s", 1.0, NOW.isoformat())


def _rel(rid, a, b, rtype, conf=1.0):
    return Relationship(rid, a, b, rtype, None, None, True, "s", conf, NOW.isoformat())


# ── salience scoring ───────────────────────────────────────────────────
def test_score_is_bounded_and_boosts_linked():
    assert 0.0 <= _score(0.5, linked=False) <= 1.0
    assert _score(0.5, linked=True) > _score(0.5, linked=False)
    assert _score(0.9, linked=True, age_days=10_000) == 1.0  # clamped


def test_score_rises_with_age_for_stale():
    young = _score(0.4, linked=False, age_days=400)
    old = _score(0.4, linked=False, age_days=4000)
    assert old > young


# ── thin-subject detector ───────────────────────────────────────────────
def test_thin_subject_flagged_only_when_known_of_and_factless():
    maya = _ent("m", "person", "Maya")
    ghost = _ent("g", "person", "Ghost")
    rich = _ent("r", "org", "Rich")
    subjects = [maya, ghost, rich]
    attrs = {"r": [_attr("r", "industry", "tech")]}  # Rich has a fact → not thin
    known_of = {"m", "r"}  # Ghost is referenced by nothing → not flagged
    signals = thin_subjects(subjects, attrs, known_of, linked={"m"})

    flagged = {s.about_id for s in signals}
    assert flagged == {"m"}  # only Maya: known of, but no facts
    sig = signals[0]
    assert sig.kind == "dangling_ref" and sig.resolution_path == "ask"  # person → ask
    assert sig.description == "identity of Maya (person)"  # neutral internal marker


def test_place_thin_gap_is_skipped():
    # Moko already knows public places — it must NOT chase "what is Australia?"
    # (that's what bloated the graph into an encyclopedia).
    oz = _ent("o", "place", "Australia")
    assert thin_subjects([oz], {}, known_of={"o"}, linked={"o"}) == []


def test_thin_subject_skips_explicit_ids():
    jordan = _ent("c", "person", "Jordan")
    signals = thin_subjects([jordan], {}, known_of={"c"}, linked=set(), skip={"c"})
    assert signals == []  # the companion is never "who is X?"


# ── unconfirmed-relationship detector ──────────────────────────────────
def test_unconfirmed_relationship_only_below_threshold():
    sure = _rel("1", "a", "b", "works_at", conf=0.9)
    unsure = _rel("2", "a", "c", "married_to", conf=0.3)
    names = {"a": "Maya", "b": "Globex", "c": "Sam"}
    signals = unconfirmed_relationships([sure, unsure], names, linked={"a"})

    assert len(signals) == 1
    assert signals[0].kind == "verification"
    assert signals[0].description == "verify: Maya married to Sam"


# ── stale-fact detector ────────────────────────────────────────────────
def test_stale_fact_flags_old_volatile_keys_only():
    old = (NOW - dt.timedelta(days=500)).isoformat()
    recent = (NOW - dt.timedelta(days=30)).isoformat()
    attrs = {
        "m": [
            _attr("m", "occupation", "designer", valid_from=old),  # volatile + old → flag
            _attr("m", "city", "Lisbon", valid_from=recent),  # volatile but fresh → no
            _attr("m", "birthday", "May 3", valid_from=old),  # old but not volatile → no
        ]
    }
    signals = stale_facts(attrs, {"m": "Maya"}, linked={"m"}, now=NOW)

    assert len(signals) == 1
    assert signals[0].kind == "stale_fact" and "occupation" in signals[0].description
    assert signals[0].description.startswith("recheck Maya's occupation")
    assert signals[0].resolution_path == "ask"


# ── orchestration against a real store ─────────────────────────────────
@pytest.fixture
async def store(tmp_path):
    s = await Store.open(tmp_path / "moko.db")
    try:
        yield s
    finally:
        await s.close()


async def test_detect_gaps_writes_and_dedups(store):
    src = await store.add_source("user_stated")
    companion = await store.add_subject("person", "Jordan")
    maya = await store.add_subject("person", "Maya")  # known of, but factless → thin
    await store.add_relationship(companion.id, maya.id, "sibling", source_id=src.id)

    created = await detect_gaps(store, companion_id=companion.id)
    assert any(g.kind == "dangling_ref" and g.about_id == maya.id for g in created)
    assert all(g.about_id != companion.id for g in created)  # companion never flagged

    # A re-scan finds the same gap and writes nothing new (idempotent).
    again = await detect_gaps(store, companion_id=companion.id)
    assert again == []
    open_now = await store.open_gaps(limit=50)
    assert len([g for g in open_now if g.about_id == maya.id]) == 1


async def test_detect_gaps_for_is_scoped_to_given_subjects(store):
    # Incremental detection only looks at the subjects it's handed — not the
    # whole graph — so the per-turn hot path stays O(changes).
    src = await store.add_source("user_stated")
    companion = await store.add_subject("person", "Jordan")
    priya = await store.add_subject("person", "Priya")  # touched this "turn", factless
    await store.add_relationship(companion.id, priya.id, "colleague_of", source_id=src.id)
    # An untouched factless subject elsewhere in the graph must NOT be scanned.
    ghost = await store.add_subject("person", "Ghost")
    await store.add_relationship(companion.id, ghost.id, "knows", source_id=src.id)

    created = await detect_gaps_for(store, [priya.id], companion_id=companion.id)
    flagged = {g.about_id for g in created}
    assert priya.id in flagged
    assert ghost.id not in flagged  # out of scope — left for the periodic scan


async def test_detect_gaps_for_empty_is_noop(store):
    assert await detect_gaps_for(store, [], companion_id=None) == []


async def test_detect_gaps_skips_non_people_and_ranks_linked_higher(store):
    src = await store.add_source("user_stated")
    companion = await store.add_subject("person", "Jordan")
    friend = await store.add_subject("person", "Wren")  # linked person → higher
    await store.add_relationship(companion.id, friend.id, "friend_of", source_id=src.id)
    sam = await store.add_subject("person", "Sam")  # unlinked person
    org = await store.add_subject("org", "Globex")  # not a person → no thin gap at all
    await store.add_relationship(sam.id, org.id, "works_at", source_id=src.id)

    created = await detect_gaps(store, companion_id=companion.id)
    assert created[0].salience == max(g.salience for g in created)  # sorted desc
    assert not any(g.about_id == org.id for g in created)  # places/orgs aren't chased
    wren = next(g for g in created if g.about_id == friend.id)
    sam_gap = next(g for g in created if g.about_id == sam.id)
    assert wren.salience > sam_gap.salience  # linked person beats unlinked person


async def test_next_curiosity_gap_prefers_unseen_then_salience(store):
    # research-path gaps are never raised in conversation (Moko answers those).
    await store.add_gap(
        "identity of Australia (place)", "dangling_ref", resolution_path="research", salience=0.9
    )
    low = await store.add_gap(
        "the speaker's brother's name", "missing_attr", resolution_path="ask", salience=0.3
    )
    high = await store.add_gap(
        "identity of Sam (person)", "dangling_ref", resolution_path="ask", salience=0.7
    )

    # Highest-salience ask-path gap first, and never the research one.
    pick = await store.next_curiosity_gap()
    assert pick.id == high.id

    # Once raised, it drops behind never-raised gaps (rotation/pacing).
    await store.mark_gap_surfaced(high.id)
    assert (await store.next_curiosity_gap()).id == low.id


async def test_resolve_satisfied_gaps_closes_filled_dangling_refs(store):
    src = await store.add_source("user_stated")
    sam = await store.add_subject("person", "Sam")
    gap = await store.add_gap("identity of Sam (person)", "dangling_ref", about_id=sam.id)

    assert await store.resolve_satisfied_gaps() == 0  # Sam still factless → stays open
    await store.set_attribute(sam.id, "occupation", "pilot", source_id=src.id)
    assert await store.resolve_satisfied_gaps() == 1  # now has a fact → resolved
    assert (await store.get_gap(gap.id)).status == "resolved"
    # Structural resolution is as visible as inline: it emits a gap.resolved event.
    from moko.events import BUS

    assert any(
        e.get("type") == "gap.resolved" and e.get("id") == gap.id for e in BUS.recent()
    )


async def test_resolve_satisfied_gaps_closes_answered_missing_attr(store):
    src = await store.add_source("user_stated")
    biscuit = await store.add_subject("pet", "Biscuit")
    age_gap = await store.add_gap("Biscuit's age", "missing_attr", about_id=biscuit.id)
    food_gap = await store.add_gap("Biscuit's favourite food", "missing_attr", about_id=biscuit.id)

    assert await store.resolve_satisfied_gaps() == 0  # nothing known yet → both open
    await store.set_attribute(biscuit.id, "age", "4", source_id=src.id)
    assert await store.resolve_satisfied_gaps() == 1  # the `age` attr answers the age gap
    assert (await store.get_gap(age_gap.id)).status == "resolved"
    assert (await store.get_gap(food_gap.id)).status == "open"  # unrelated stays open


async def test_resolve_satisfied_gaps_ignores_short_or_preexisting_attrs(store):
    # An attribute already present when the gap opened (or a too-short key) must
    # not wrongly close a question.
    src = await store.add_source("user_stated")
    e = await store.add_subject("person", "Mara")
    await store.set_attribute(e.id, "do", "things", source_id=src.id)  # key length < 3
    gap = await store.add_gap("what does Mara do for work", "missing_attr", about_id=e.id)
    assert await store.resolve_satisfied_gaps() == 0  # short key + pre-existing → not closed
    assert (await store.get_gap(gap.id)).status == "open"


async def test_next_research_gap_picks_research_path_only(store):
    await store.add_gap(
        "identity of Sam (person)", "dangling_ref", resolution_path="ask", salience=0.9
    )
    research = await store.add_gap(
        "identity of Lisbon (place)", "dangling_ref", resolution_path="research", salience=0.4
    )
    pick = await store.next_research_gap()
    assert pick is not None and pick.id == research.id  # never the ask-path one


async def test_gap_status_lifecycle(store):
    gap = await store.add_gap("Who is Maya?", "dangling_ref", resolution_path="ask")
    await store.set_gap_status(gap.id, "asked")
    assert (await store.get_gap(gap.id)).status == "asked"
    assert gap.id not in {g.id for g in await store.open_gaps()}  # no longer "open"

    # The status stamp lands in the DB (the model doesn't surface it; check raw).
    cur = await store.conn.execute(
        "SELECT last_asked_at FROM knowledge_gaps WHERE id = ?", (gap.id,)
    )
    assert (await cur.fetchone())["last_asked_at"] is not None

    src = await store.add_source("web")
    await store.set_gap_status(gap.id, "resolved", resolved_by=src.id)
    assert (await store.get_gap(gap.id)).status == "resolved"


async def test_list_gaps_shows_all_statuses_active_first(store):
    # The monitor's gaps browser lists ALL gaps (so completed/archived ones stay visible and
    # the tab badge matches the rows), with active ones ahead of resolved/abandoned. This is
    # distinct from `open_gaps`, the brain's actionable worklist (open only).
    await store.add_gap("still curious about X", "missing_attr", salience=0.5)
    b = await store.add_gap("was curious about Y", "missing_attr", salience=0.9)
    c = await store.add_gap("gave up on Z", "missing_attr", salience=0.8)
    await store.set_gap_status(b.id, "resolved")
    await store.set_gap_status(c.id, "abandoned")

    descs = [g.description for g in await store.list_gaps(limit=50)]
    assert set(descs) == {"still curious about X", "was curious about Y", "gave up on Z"}  # all
    assert descs[0] == "still curious about X"  # the active (open) one leads
    assert descs[1] == "was curious about Y"  # then closed, most-salient first (resolved 0.9)
    # The brain's actionable worklist still shows only the open one — unchanged.
    assert [g.description for g in await store.open_gaps()] == ["still curious about X"]


async def test_gap_detection_dedups_against_resolved_gaps(store):
    # A resolved dangling-ref gap must NOT be re-created while the subject stays thin — the bug
    # that piled up N identical resolved gaps for the same person. Dedup is against ALL statuses.
    src = await store.add_source("user_stated")
    jordan = await store.add_subject("person", "Jordan")
    tom = await store.add_subject("person", "Tom")  # thin: known of, no facts
    await store.add_relationship(jordan.id, tom.id, "business_partner", source_id=src.id)

    first = await detect_gaps(store, companion_id=jordan.id)
    tom_gaps = [g for g in first if "Tom" in g.description]
    assert len(tom_gaps) == 1  # created once
    await store.set_gap_status(tom_gaps[0].id, "resolved")

    # Tom is still thin → detection runs again, but must NOT re-create the (resolved) gap.
    again = await detect_gaps(store, companion_id=jordan.id)
    assert not any("Tom" in g.description for g in again)
    all_tom = [g for g in await store.list_gaps(limit=100) if "Tom" in g.description]
    assert len(all_tom) == 1  # exactly one row, not a resolved + a fresh open duplicate
