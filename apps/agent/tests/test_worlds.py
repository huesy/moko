"""M11 — worlds: subjects belong to a world (the companion's vs Moko's).

The load-bearing bits: two singleton worlds are seeded; new subjects default to the
companion's world; resolution + merge-candidate flagging are scoped per world, so "the
companion's kitchen" and "Moko's kitchen" are distinct nodes that never merge.
"""

import pytest

from moko.store import COMPANION_WORLD, MOKO_WORLD, Store


@pytest.fixture
async def store(tmp_path):
    s = await Store.open(tmp_path / "moko.db")
    try:
        yield s
    finally:
        await s.close()


async def test_two_worlds_are_seeded(store):
    cur = await store.conn.execute("SELECT kind FROM worlds ORDER BY kind")
    assert [r["kind"] for r in await cur.fetchall()] == ["companion", "moko"]


async def test_new_subjects_default_to_the_companion_world(store):
    s = await store.add_subject("person", "Priya")
    assert s.world_id == COMPANION_WORLD


async def test_a_subject_can_be_placed_in_mokos_world(store):
    s = await store.add_subject("place", "the living room", world_id=MOKO_WORLD)
    assert s.world_id == MOKO_WORLD


async def test_same_name_in_different_worlds_stays_distinct(store):
    a = await store.add_subject("place", "the kitchen", world_id=COMPANION_WORLD)
    b = await store.add_subject("place", "the kitchen", world_id=MOKO_WORLD)
    assert a.id != b.id  # the companion's kitchen and Moko's are genuinely different places
    # ...and a same-name clash across worlds is NOT flagged for the dream to merge.
    cur = await store.conn.execute("SELECT COUNT(*) AS c FROM merge_candidates")
    assert (await cur.fetchone())["c"] == 0


async def test_same_name_same_world_resolves_to_one_node(store):
    a = await store.add_subject("place", "the kitchen", world_id=MOKO_WORLD)
    b = await store.add_subject("place", "the kitchen", world_id=MOKO_WORLD)
    assert a.id == b.id  # one node, resolved (dedup within a world)


async def test_resolve_is_world_scoped(store):
    await store.add_subject("place", "the den", world_id=MOKO_WORLD)
    assert await store.resolve("place", "the den", world_id=MOKO_WORLD) is not None
    assert await store.resolve("place", "the den") is None  # companion world: not there


async def test_find_by_name_is_world_scoped(store):
    await store.add_subject("place", "the den", world_id=MOKO_WORLD)
    assert await store.find_by_name("the den", world_id=MOKO_WORLD) is not None
    assert await store.find_by_name("the den", world_id=COMPANION_WORLD) is None
    assert await store.find_by_name("the den") is not None  # world-agnostic still works


async def test_recall_is_world_scoped(store):
    # A "kitchen" in each world; recall returns only the one in the requested world.
    src = await store.add_source("test")
    cw = await store.add_subject("place", "the kitchen", world_id=COMPANION_WORLD)
    mw = await store.add_subject("place", "the kitchen", world_id=MOKO_WORLD)
    await store.set_attribute(cw.id, "note", "granite counters", source_id=src.id)
    await store.set_attribute(mw.id, "note", "wooden counters", source_id=src.id)

    comp = await store.recall("kitchen", world_id=COMPANION_WORLD)
    comp_subjects = {h.ref_id for h in comp if h.kind == "subject"}
    assert cw.id in comp_subjects and mw.id not in comp_subjects

    mine = await store.recall("kitchen", world_id=MOKO_WORLD)
    mine_subjects = {h.ref_id for h in mine if h.kind == "subject"}
    assert mw.id in mine_subjects and cw.id not in mine_subjects


async def test_lookup_tool_respects_the_world_arg(store):
    from moko.agent.memory_tools import make_executor

    await store.add_subject("pet", "Mochi", world_id=MOKO_WORLD)  # Moko's own cat
    ex = make_executor(store)
    assert "Mochi" in await ex("lookup", {"name": "Mochi", "world": "mine"})
    # Default world is the companion's — where Moko has no such pet.
    assert "no record" in (await ex("lookup", {"name": "Mochi"})).lower()


async def test_companion_curiosity_ignores_mokos_world(store):
    from moko.agent.curiosity import detect_gaps_for

    pet = await store.add_subject("pet", "the cat", world_id=MOKO_WORLD)
    # A turn that touched a Moko's-world subject must not spawn an "ask the companion" gap.
    assert await detect_gaps_for(store, [pet.id], companion_id=None) == []


async def test_thoughts_and_gaps_carry_and_filter_by_world(store):
    # Thoughts default to the companion's world; a Moko's-world thought is filtered apart.
    await store.add_thought("feeling", "Andrew seemed tired")  # default companion
    await store.add_thought("feeling", "my kitchen is cozy", world_id=MOKO_WORLD)
    comp = await store.list_thoughts(world_id=COMPANION_WORLD)
    moko = await store.list_thoughts(world_id=MOKO_WORLD)
    assert [t.body for t in comp] == ["Andrew seemed tired"]
    assert [t.body for t in moko] == ["my kitchen is cozy"]
    assert len(await store.list_thoughts()) == 2  # unscoped sees both

    # Gaps carry a world too.
    await store.add_gap("who is Priya?", "identity")  # default companion
    await store.add_gap("what's in the attic?", "exploration", world_id=MOKO_WORLD)
    assert len(await store.open_gaps(world_id=COMPANION_WORLD)) == 1
    assert (await store.open_gaps(world_id=MOKO_WORLD))[0].description == "what's in the attic?"

