"""Feature 2 — backup + restore of the whole mind.

The graph is irreplaceable, so these guard the parts that could lose it: a
restore must never apply a corrupt snapshot, must back up the current mind
first, and auto-pruning must never delete the operator's deliberate snapshots.
"""

import os

import pytest

from moko.store import Store, backup


def _db(tmp_path):
    return tmp_path / "moko.db"


async def test_create_lists_and_classifies_snapshot(tmp_path):
    store = await Store.open(_db(tmp_path))
    try:
        await store.add_subject("person", "Alice")
        await backup.create_snapshot(store.conn, _db(tmp_path), "before-trip")
    finally:
        await store.close()
    snaps = backup.list_snapshots(_db(tmp_path))
    assert [s.name for s in snaps] == ["before-trip"]
    assert snaps[0].kind == "manual" and snaps[0].size > 0


async def test_restore_round_trip_undoes_changes_and_backs_up_current(tmp_path):
    # Snapshot a mind that knows Alice, then learn Bob, then restore: Bob is gone,
    # Alice is back, and the pre-restore state was preserved (reversible).
    store = await Store.open(_db(tmp_path))
    await store.add_subject("person", "Alice")
    await backup.create_snapshot(store.conn, _db(tmp_path), "checkpoint")
    await store.add_subject("person", "Bob")
    await store.close()  # the daemon would be stopped before a restore

    summary = await backup.restore_snapshot(_db(tmp_path), "checkpoint")
    assert "checkpoint" in summary

    store = await Store.open(_db(tmp_path))
    try:
        assert await store.find_by_name("Alice") is not None  # the snapshot's state
        assert await store.find_by_name("Bob") is None  # the post-snapshot change is undone
    finally:
        await store.close()
    # The mind that existed at restore time (which knew Bob) was backed up first.
    kinds = {s.kind for s in backup.list_snapshots(_db(tmp_path))}
    assert "pre-restore" in kinds


async def test_restore_refuses_a_corrupt_snapshot_and_leaves_the_mind_intact(tmp_path):
    store = await Store.open(_db(tmp_path))
    await store.add_subject("person", "Alice")
    await store.close()

    snap_dir = backup.snapshots_dir(_db(tmp_path))
    snap_dir.mkdir(parents=True, exist_ok=True)
    (snap_dir / "junk.db").write_bytes(b"this is not a database")

    assert not await backup.integrity_ok(snap_dir / "junk.db")
    with pytest.raises(ValueError):
        await backup.restore_snapshot(_db(tmp_path), "junk")

    # The live mind is untouched — a refused restore must change nothing.
    store = await Store.open(_db(tmp_path))
    try:
        assert await store.find_by_name("Alice") is not None
    finally:
        await store.close()


async def test_restore_rejects_a_valid_db_that_isnt_a_mind(tmp_path):
    import aiosqlite

    store = await Store.open(_db(tmp_path))
    await store.close()
    snap_dir = backup.snapshots_dir(_db(tmp_path))
    snap_dir.mkdir(parents=True, exist_ok=True)
    # A perfectly valid SQLite file, but not Moko's graph.
    other = snap_dir / "stranger.db"
    conn = await aiosqlite.connect(str(other))
    await conn.execute("CREATE TABLE whatever (x)")
    await conn.commit()
    await conn.close()

    assert await backup.integrity_ok(other)  # it IS a valid db …
    with pytest.raises(ValueError):  # … but not a mind, so restore refuses
        await backup.restore_snapshot(_db(tmp_path), "stranger")


async def test_missing_snapshot_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        await backup.restore_snapshot(_db(tmp_path), "nope")


def test_prune_only_touches_its_own_prefix(tmp_path):
    d = backup.snapshots_dir(_db(tmp_path))
    d.mkdir(parents=True)
    for i in range(7):  # 7 auto snapshots, oldest → newest
        p = d / f"pre-v{i}.db"
        p.write_text("x")
        os.utime(p, (i, i))
    for name, t in (("manual-a.db", 100), ("before-trip.db", 101)):
        p = d / name
        p.write_text("x")
        os.utime(p, (t, t))

    backup.prune(_db(tmp_path), backup.AUTO_PREFIX, 5)
    remaining = {p.name for p in d.glob("*.db")}

    assert "pre-v0.db" not in remaining and "pre-v1.db" not in remaining  # oldest 2 pruned
    assert len([n for n in remaining if n.startswith("pre-v")]) == 5  # kept the newest 5
    assert {"manual-a.db", "before-trip.db"} <= remaining  # deliberate snapshots survive


def test_sanitize_label_blocks_traversal_and_spaces(tmp_path):
    assert "/" not in backup.sanitize_label("../../etc/passwd")
    assert " " not in backup.sanitize_label("my big backup")
    assert backup.sanitize_label("   ") .startswith(backup.MANUAL_PREFIX)  # empty → stamped


def test_kind_classification():
    assert backup.kind_of("pre-v9.db") == "auto"
    assert backup.kind_of("pre-restore-x.db") == "pre-restore"
    assert backup.kind_of("anything-else.db") == "manual"


async def test_snapshot_now_without_a_db_is_a_clean_error(tmp_path):
    with pytest.raises(FileNotFoundError):
        await backup.snapshot_now(_db(tmp_path), "x")


# ── CLI guards ────────────────────────────────────────────────────────────────
def test_restore_refuses_while_daemon_is_live(tmp_path, monkeypatch):
    from moko.cli import restore

    monkeypatch.setattr(restore._daemon, "is_daemon_live", lambda: True)
    assert restore.run("anything", assume_yes=True) == 1  # refuses; daemon owns the DB


def test_restore_with_no_name_lists_and_signals_usage(tmp_path):
    from moko.cli import restore

    assert restore.run(None) == 1  # nothing to do but show what's available


def test_snapshot_list_is_friendly_when_empty(tmp_path):
    from moko.cli import snapshot

    assert snapshot.run(list_all=True) == 0
