"""Property-style store tests — plain `pytest.mark.parametrize`, no hypothesis.

Each test states an *invariant* and drives it over a spread of inputs (case,
Unicode-accent, and endpoint permutations) rather than a single example, so the
store's dedup/supersede guarantees are exercised the way property-based tests
would — deterministically, with the standard toolchain. `integrity.check` runs
at the end of each as a secondary oracle that the graph is internally consistent.
"""

import pytest

from moko.store import Store, integrity


@pytest.fixture
async def store(tmp_path):
    s = await Store.open(tmp_path / "moko.db")
    try:
        yield s
    finally:
        await s.close()


async def _assert_clean(store):
    """No integrity finding is an error, as a secondary oracle."""
    for f in await integrity.check(store.conn):
        assert f.severity != "error" or f.ok, f"{f.name}: {f.detail}"


# ── (a) add_subject dedup is idempotent over case + Unicode-accent forms ──────
# The bug this proves fixed: SQLite `lower()` is ASCII-only, so "Müller"/"MÜLLER"
# used to miss the strong-match and mint duplicate subjects. With Unicode casefold
# on both sides, every case/accent-preserving form of one name resolves to ONE node.
@pytest.mark.parametrize(
    "forms",
    [
        ["Müller", "müller", "MÜLLER", "MÜLLER"],
        ["Élodie", "élodie", "ÉLODIE"],
        ["Straße", "straße", "STRASSE"],  # casefold folds ß → "ss"
        ["Renée", "renée", "RENÉE"],
        ["Ångström", "ångström", "ÅNGSTRÖM"],
        ["María José", "maría josé", "MARÍA JOSÉ"],
    ],
)
async def test_add_subject_dedup_is_case_and_accent_idempotent(store, forms):
    ids = set()
    for name in forms:
        subj = await store.add_subject("person", name)
        ids.add(subj.id)
    assert len(ids) == 1, f"{forms} should resolve to one subject, got {len(ids)}"

    # And the canonical count agrees: exactly one live person subject exists.
    cur = await store.conn.execute(
        "SELECT count(*) AS n FROM subjects WHERE kind = 'person' AND merged_into IS NULL"
    )
    assert (await cur.fetchone())["n"] == 1

    # `resolve` finds the same node from any form, too (alias-free strong match).
    for name in forms:
        got = await store.resolve("person", name)
        assert got is not None and got.id in ids
    await _assert_clean(store)


# ── (b) a supersede chain yields exactly one current value + full history ──────
@pytest.mark.parametrize(
    "values",
    [
        ["Boston", "NYC"],
        ["Boston", "NYC", "Chicago"],
        ["a", "b", "c", "d", "e"],
        ["Tokyo", "Osaka", "Tokyo"],  # returning to an earlier value still supersedes
    ],
)
async def test_supersede_chain_one_current_full_history(store, values):
    src = await store.add_source("user_stated")
    e = await store.add_subject("person", "Subject")
    for v in values:
        await store.set_attribute(e.id, "city", v, source_id=src.id)

    current = await store.attributes_for(e.id, current_only=True)
    assert len(current) == 1
    assert current[0].value == values[-1]  # the last write is the live one
    assert current[0].valid_to is None

    # History retains every distinct write (an unchanged restate doesn't churn, so
    # count consecutive-deduplicated writes — never more than len(values)).
    expected_rows = 1
    for i in range(1, len(values)):
        if values[i] != values[i - 1]:
            expected_rows += 1
    allrows = await store.attributes_for(e.id, current_only=False)
    assert len(allrows) == expected_rows
    superseded = [a for a in allrows if a.valid_to is not None]
    assert len(superseded) == expected_rows - 1  # all but the current are closed
    await _assert_clean(store)


# ── (c) a symmetric relationship dedups under endpoint permutation ────────────
@pytest.mark.parametrize(
    "pairs",
    [
        [("A", "B"), ("B", "A")],
        [("A", "B"), ("A", "B")],
        [("A", "B"), ("B", "A"), ("A", "B")],
    ],
)
async def test_symmetric_relationship_dedup_under_permutation(store, pairs):
    src = await store.add_source("user_stated")
    a = await store.add_subject("person", "A")
    b = await store.add_subject("person", "B")
    node = {"A": a.id, "B": b.id}
    for frm, to in pairs:
        await store.add_relationship(
            node[frm], node[to], "friend_of", source_id=src.id, directed=False
        )
    # However the endpoints are stated/ordered, one undirected edge exists.
    assert len(await store.relationships_for(a.id)) == 1
    assert len(await store.relationships_for(b.id)) == 1
    await _assert_clean(store)
