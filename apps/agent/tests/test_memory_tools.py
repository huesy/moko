import pytest

from moko.agent.memory_tools import MEMORY_TOOLS, make_executor
from moko.store import Store


@pytest.fixture
async def store(tmp_path):
    s = await Store.open(tmp_path / "moko.db")
    try:
        yield s
    finally:
        await s.close()


def test_tool_set():
    assert {t.name for t in MEMORY_TOOLS} == {"recall", "lookup", "reflect"}


async def test_lookup_returns_attrs_and_relationships(store):
    src = await store.add_source("user_stated")
    andy = await store.add_subject("person", "Andy")
    tara = await store.add_subject("person", "Tara")
    await store.add_relationship(andy.id, tara.id, "has_sister", source_id=src.id)
    await store.set_attribute(tara.id, "residence", "Brisbane", source_id=src.id)

    out = await make_executor(store)("lookup", {"name": "Tara"})
    assert "Tara (person)" in out
    assert "residence: Brisbane" in out
    assert "Andy has_sister Tara" in out  # multi-hop, from Tara's side


async def test_lookup_surfaces_linked_thoughts(store):
    biscuit = await store.add_subject("pet", "Biscuit")
    await store.add_thought(
        "feeling", "I've grown fond of Biscuit", links=[("subject", biscuit.id)]
    )
    out = await make_executor(store)("lookup", {"name": "Biscuit"})
    assert "Your private thoughts about Biscuit:" in out
    assert "grown fond of Biscuit" in out


async def test_lookup_shows_fact_provenance(store):
    # Provenance-aware recall: a web-read fact is flagged so Moko hedges it, while
    # the companion's own word stays clean (the trusted default).
    e = await store.add_subject("person", "Dana")
    web = await store.add_source("web", ref="http://example/dana")
    said = await store.add_source("user_stated")
    await store.set_attribute(e.id, "employer", "NewCo", source_id=web.id, confidence=0.6)
    await store.set_attribute(e.id, "city", "Oslo", source_id=said.id)
    out = await make_executor(store)("lookup", {"name": "Dana"})
    employer_line = next(ln for ln in out.splitlines() if "employer" in ln)
    city_line = next(ln for ln in out.splitlines() if "city" in ln)
    assert "from the web" in employer_line  # second-hand → flagged
    assert "from the web" not in city_line and "(" not in city_line  # companion's word → clean


async def test_lookup_shows_relationship_provenance(store):
    # The provenance floor applies to edges too: an inferred/web-read relationship is
    # hedged, while one the companion stated stays clean.
    web = await store.add_source("web", ref="http://x/ana")
    said = await store.add_source("user_stated")
    ana = await store.add_subject("person", "Ana")
    globex = await store.add_subject("org", "Globex")
    pat = await store.add_subject("person", "Pat")
    await store.add_relationship(ana.id, globex.id, "works_at", source_id=web.id, confidence=0.5)
    await store.add_relationship(ana.id, pat.id, "friend_of", source_id=said.id)

    out = await make_executor(store)("lookup", {"name": "Ana"})
    works_line = next(ln for ln in out.splitlines() if "works_at" in ln)
    friend_line = next(ln for ln in out.splitlines() if "friend_of" in ln)
    assert "from the web" in works_line and "conf 0.5" in works_line  # second-hand → hedged
    assert "(" not in friend_line  # companion's own word → clean


async def test_lookup_fact_meta_shows_confidence_and_age(store):
    # The honesty tag carries confidence and age, not just the source phrase, so a
    # shaky or old belief reads as a belief, not settled truth.
    import datetime as dt

    from moko.clock import now_utc

    e = await store.add_subject("person", "Dana")
    web = await store.add_source("web", ref="http://x/dana")
    await store.set_attribute(e.id, "employer", "NewCo", source_id=web.id, confidence=0.6)
    old = (now_utc() - dt.timedelta(days=40)).isoformat()
    await store.conn.execute(
        "UPDATE attributes SET valid_from = ? WHERE subject_id = ? AND key = 'employer'",
        (old, e.id),
    )
    await store.conn.commit()

    line = next(
        ln for ln in (await make_executor(store)("lookup", {"name": "Dana"})).splitlines()
        if "employer" in ln
    )
    assert "conf 0.6" in line  # uncertainty is shown
    assert "mo ago" in line  # and the belief's age


async def test_lookup_labels_thought_shareability(store):
    # The honesty boundary travels with each surfaced thought: a private one is
    # unmarked (keep it), a shareable one is flagged voiceable.
    e = await store.add_subject("person", "Mira")
    await store.add_thought("opinion", "a guarded private worry", links=[("subject", e.id)])
    await store.add_thought(
        "feeling", "a warm shareable note", shareability="shareable", links=[("subject", e.id)]
    )
    out = await make_executor(store)("lookup", {"name": "Mira"})
    worry_line = next(ln for ln in out.splitlines() if "guarded private worry" in ln)
    warm_line = next(ln for ln in out.splitlines() if "warm shareable note" in ln)
    assert "shareable" not in worry_line  # private → unmarked
    assert "shareable" in warm_line  # voiceable → flagged


async def test_reflect_about_a_topic(store):
    paris = await store.add_subject("place", "Paris")
    await store.add_thought(
        "opinion", "Paris seemed gloomy in the rain", links=[("subject", paris.id)]
    )
    execute = make_executor(store)
    # FTS over thought bodies — finds it whether or not it was linked.
    out = await execute("reflect", {"about": "Paris"})
    assert "gloomy" in out
    # A topic with no thoughts on record says so, rather than inventing one.
    miss = await execute("reflect", {"about": "Reykjavik"})
    assert "haven't formed any" in miss.lower()


async def test_reflect_recent_when_no_topic(store):
    await store.add_thought("feeling", "today felt warm and easy")
    out = await make_executor(store)("reflect", {})
    assert "on your mind lately" in out.lower()
    assert "warm and easy" in out


async def test_reflect_empty_mind(store):
    out = await make_executor(store)("reflect", {})
    assert "quiet" in out.lower()


async def test_lookup_unknown(store):
    out = await make_executor(store)("lookup", {"name": "Nobody"})
    assert "no record" in out.lower()


async def test_recall_finds_and_misses(store):
    await store.add_subject("place", "Lisbon")
    execute = make_executor(store)
    assert "Lisbon" in await execute("recall", {"query": "Lisbon"})
    assert "nothing" in (await execute("recall", {"query": "zzzznope"})).lower()


async def test_unknown_tool_is_handled(store):
    out = await make_executor(store)("frobnicate", {})
    assert "unknown" in out.lower()


async def test_lookup_shows_recent_observations(store):
    src = await store.add_source("user_stated")
    me = await store.add_subject("person", "Andrew")
    await store.add_observation(
        me.id, "sleep_hours", "6.0", source_id=src.id,
        value_type="number", observed_at="2026-06-28T07:00:00+00:00",
    )
    out = await make_executor(store)("lookup", {"name": "Andrew"})
    assert "sleep_hours: 6.0" in out


async def test_recall_prefixes_attribute_hit_with_owner(store):
    # A bare "age: 7" recall hit is useless — whose? The owning subject's name is
    # resolved at render time and prefixed, without baking it into the FTS content.
    src = await store.add_source("user_stated")
    biscuit = await store.add_subject("pet", "Biscuit")
    await store.set_attribute(biscuit.id, "age", "7", source_id=src.id, value_type="number")
    out = await make_executor(store)("recall", {"query": "age"})
    line = next(ln for ln in out.splitlines() if "age: 7" in ln)
    assert "(attribute)" in line
    assert "Biscuit" in line  # the owner is named, so the fact isn't orphaned


async def test_recall_prefixes_media_hit_with_owner(store):
    # A media hit's description is likewise anonymous; its owner is the speaker of the
    # message it rode in on, resolved at render time.
    mira = await store.add_subject("person", "Mira")
    msg = await store.add_message("telegram", "in", "look at this", speaker_id=mira.id)
    await store.add_media(
        b"\xff\xd8\xffbytes", kind="image", mime="image/jpeg",
        description="a sunlit greenhouse full of ferns", message_id=msg.id,
    )
    out = await make_executor(store)("recall", {"query": "greenhouse ferns"})
    line = next(ln for ln in out.splitlines() if "greenhouse" in ln)
    assert "(media)" in line
    assert "Mira" in line  # named after the message's speaker


async def test_lookup_shows_superseded_history(store):
    # Supersede-not-delete keeps past values; lookup surfaces them (time-anchored) so Moko can
    # answer "what was X before?" — not only the current value. (The reported bug: it said it
    # didn't know Maya used to be a nurse after she became a doctor.)
    src = await store.add_source("user_stated")
    maya = await store.add_subject("person", "Maya")
    await store.set_attribute(maya.id, "occupation", "nurse", source_id=src.id)
    await store.set_attribute(maya.id, "occupation", "doctor", source_id=src.id)  # supersedes

    out = await make_executor(store)("lookup", {"name": "Maya"})
    line = next(ln for ln in out.splitlines() if ln.startswith("- occupation:"))
    assert "occupation: doctor" in line  # the current value leads
    assert "←" in line  # …then the inline history chain
    assert "nurse (until " in line  # …the superseded value, dated (when it stopped being true)
