"""Feature 3 — the mind self-doctor (store.integrity).

These prove the structural checks fire on the failure modes that matter most:
a fact that lost its source (the provenance floor), two 'current' values for one
fact (a missed supersede), a stale fact still in recall, a crashed dream. A clean
mind must pass every check — that's what makes a failure meaningful.
"""

import datetime as dt

import pytest

from moko.clock import now_utc
from moko.store import Store, integrity


@pytest.fixture
async def store(tmp_path):
    s = await Store.open(tmp_path / "moko.db")
    try:
        yield s
    finally:
        await s.close()


def _by_name(findings):
    return {f.name: f for f in findings}


async def test_clean_mind_passes_every_check(store):
    src = await store.add_source("user_stated")
    a = await store.add_subject("person", "Ada")
    b = await store.add_subject("org", "Acme")
    await store.set_attribute(a.id, "city", "Berlin", source_id=src.id)
    await store.add_relationship(a.id, b.id, "works_at", source_id=src.id)

    findings = await integrity.check(store.conn)
    assert all(f.ok for f in findings), [f.name for f in findings if not f.ok]
    assert _by_name(findings)["Provenance"].ok  # the core promise, explicitly checked


async def test_detects_dangling_reference_and_lost_provenance(store):
    a = await store.add_subject("person", "Ada")
    # Forge a fact whose source doesn't exist — only possible with FK enforcement off.
    await store.conn.execute("PRAGMA foreign_keys = OFF")
    await store.conn.execute(
        "INSERT INTO attributes"
        " (id, subject_id, key, value, value_type, source_id, confidence, created_at)"
        " VALUES ('attX', ?, 'city', 'Nowhere', 'string', 'ghost-src', 1.0, ?)",
        (a.id, now_utc().isoformat()),
    )
    await store.conn.commit()

    by = _by_name(await integrity.check(store.conn))
    assert by["References"].count >= 1 and by["References"].severity == "error"
    assert by["Provenance"].count >= 1  # the explicit provenance-floor scan catches it too


async def test_detects_two_current_values_for_one_fact(store):
    src = await store.add_source("user_stated")
    a = await store.add_subject("person", "Ada")
    await store.set_attribute(a.id, "city", "Berlin", source_id=src.id)
    # A second CURRENT value for the same key — a supersede that never happened.
    await store.conn.execute(
        "INSERT INTO attributes"
        " (id, subject_id, key, value, value_type, source_id, confidence, created_at)"
        " VALUES ('dup1', ?, 'city', 'Munich', 'string', ?, 1.0, ?)",
        (a.id, src.id, now_utc().isoformat()),
    )
    await store.conn.commit()

    f = _by_name(await integrity.check(store.conn))["Current attributes"]
    assert f.count >= 1 and f.severity == "error"


async def test_detects_multiple_active_sessions(store):
    now = now_utc().isoformat()
    for sid in ("s1", "s2"):
        await store.conn.execute(
            "INSERT INTO sessions (id, started_at, started_reason) VALUES (?, ?, 'user')",
            (sid, now),
        )
    await store.conn.commit()

    f = _by_name(await integrity.check(store.conn))["Active session"]
    assert f.count >= 1 and f.severity == "error"


async def test_detects_stale_fact_left_in_recall(store):
    src = await store.add_source("user_stated")
    a = await store.add_subject("person", "Ada")
    now = now_utc().isoformat()
    # A superseded attribute (valid_to set) that was never removed from the FTS index.
    await store.conn.execute(
        "INSERT INTO attributes"
        " (id, subject_id, key, value, value_type, valid_to, source_id, confidence, created_at)"
        " VALUES ('stale1', ?, 'city', 'OldTown', 'string', ?, ?, 1.0, ?)",
        (a.id, now, src.id, now),
    )
    await store.conn.execute(
        "INSERT INTO search (content, kind, ref_id) VALUES ('OldTown', 'attribute', 'stale1')"
    )
    await store.conn.commit()

    f = _by_name(await integrity.check(store.conn))["Recall freshness"]
    assert f.count >= 1 and f.severity == "warn"


async def test_detects_dangling_thought_link(store):
    t = await store.add_thought("note", "about a ghost")
    await store.conn.execute(
        "INSERT INTO thought_links (id, thought_id, target_kind, target_id)"
        " VALUES ('tl1', ?, 'subject', 'ghost-subject')",
        (t.id,),
    )
    await store.conn.commit()

    f = _by_name(await integrity.check(store.conn))["Thought links"]
    assert f.count >= 1 and f.severity == "warn"


async def test_orphaned_dream_flagged_but_running_dream_is_not(store):
    long_ago = (now_utc() - dt.timedelta(days=2)).isoformat()
    await store.conn.execute(
        "INSERT INTO dreams (id, started_at, summary) VALUES ('d_old', ?, '')", (long_ago,)
    )
    await store.conn.execute(
        "INSERT INTO dreams (id, started_at, summary) VALUES ('d_now', ?, '')",
        (now_utc().isoformat(),),
    )
    await store.conn.commit()

    f = _by_name(await integrity.check(store.conn))["Dream health"]
    assert f.count == 1  # the old one is flagged; the one that just started is not


# ── CLI glue ──────────────────────────────────────────────────────────────────
def test_integrity_findings_is_none_without_a_db():
    from moko.cli import doctor

    assert doctor._integrity_findings() is None


def test_integrity_findings_lists_for_a_real_db():
    import asyncio

    from moko.config.paths import ensure_home, moko_home

    async def _make():
        s = await Store.open(moko_home() / "moko.db")
        await s.close()

    ensure_home()  # create MOKO_HOME, as the real CLI does before opening the DB
    asyncio.run(_make())
    from moko.cli import doctor

    findings = doctor._integrity_findings()
    assert findings is not None and all(f.ok for f in findings)


async def test_observations_keep_provenance_and_dont_trip_attribute_invariant(store):
    """A series of readings under one key is correct, not a duplicate-current bug:
    observations are sourced (provenance holds) and are exempt from the
    one-current-value-per-(subject,key) rule by design."""
    src = await store.add_source("user_stated")
    me = await store.add_subject("person", "Andrew")
    await store.add_observation(
        me.id, "sleep_hours", "7", source_id=src.id, observed_at="2026-06-27T07:00:00+00:00"
    )
    await store.add_observation(
        me.id, "sleep_hours", "6", source_id=src.id, observed_at="2026-06-28T07:00:00+00:00"
    )
    by = _by_name(await integrity.check(store.conn))
    assert by["Provenance"].count == 0
    assert by["Current attributes"].count == 0
