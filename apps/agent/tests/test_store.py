import pytest

from moko.store import Store


@pytest.fixture
async def store(tmp_path):
    s = await Store.open(tmp_path / "moko.db")
    try:
        yield s
    finally:
        await s.close()


async def test_open_applies_migrations(store):
    cur = await store.conn.execute("SELECT version FROM schema_migrations")
    versions = {row[0] for row in await cur.fetchall()}
    assert 1 in versions


async def test_connection_tuned_for_concurrent_access(store):
    # WAL + a busy_timeout are what let a `moko ...` CLI command write to the same
    # DB as the live daemon without an instant "database is locked" — it waits.
    mode = await (await store.conn.execute("PRAGMA journal_mode")).fetchone()
    assert mode[0].lower() == "wal"
    timeout = await (await store.conn.execute("PRAGMA busy_timeout")).fetchone()
    assert timeout[0] >= 1000


async def test_subject_dedup_strong_match(store):
    a = await store.add_subject("person", "Maya")
    b = await store.add_subject("person", "maya")  # case-insensitive strong match
    assert a.id == b.id


async def test_alias_resolution(store):
    maya = await store.add_subject("person", "Maya Tanaka")
    await store.add_alias(maya.id, "Maya")
    resolved = await store.resolve("person", "Maya")
    assert resolved is not None
    assert resolved.id == maya.id


async def test_attribute_requires_source(store):
    maya = await store.add_subject("person", "Maya")
    with pytest.raises(ValueError):
        await store.set_attribute(maya.id, "birthday", "1990-07-15", source_id="")


async def test_relationship_requires_source(store):
    # Floor #1: every relationship is sourced — symmetric to the attribute guard.
    a = await store.add_subject("person", "A")
    b = await store.add_subject("person", "B")
    with pytest.raises(ValueError):
        await store.add_relationship(a.id, b.id, "friend_of", source_id="")


async def test_writes_persist_utc_timestamps(store):
    # Floor: timestamps are stored in UTC. Prove the store actually does it, not
    # just that the clock helper can.
    import datetime as dt

    e = await store.add_subject("person", "Tz")
    src = await store.add_source("user_stated")
    await store.set_attribute(e.id, "city", "Oslo", source_id=src.id)
    msg = await store.add_message("cli", "in", "hi", speaker_id=e.id)
    stamps = []
    for sql, rid in (
        ("SELECT created_at AS t FROM sources WHERE id=?", src.id),
        ("SELECT created_at AS t FROM attributes WHERE subject_id=?", e.id),
        ("SELECT sent_at AS t FROM messages WHERE id=?", msg.id),
    ):
        cur = await store.conn.execute(sql, (rid,))
        stamps.append((await cur.fetchone())["t"])
    for ts in stamps:
        parsed = dt.datetime.fromisoformat(ts)
        assert parsed.tzinfo is not None and parsed.utcoffset() == dt.timedelta(0)


async def test_attribute_supersede_keeps_history(store):
    src = await store.add_source("user_stated")
    maya = await store.add_subject("person", "Maya")
    await store.set_attribute(maya.id, "city", "Osaka", source_id=src.id)
    await store.set_attribute(maya.id, "city", "Tokyo", source_id=src.id)

    current = await store.attributes_for(maya.id)
    assert len(current) == 1
    assert current[0].value == "Tokyo"
    assert current[0].valid_to is None

    history = await store.attributes_for(maya.id, current_only=False)
    assert len(history) == 2
    osaka = next(a for a in history if a.value == "Osaka")
    assert osaka.valid_to is not None  # superseded, not deleted


async def test_relationship_and_slice(store):
    src = await store.add_source("user_stated")
    maya = await store.add_subject("person", "Maya")
    sam = await store.add_subject("person", "Sam")
    await store.add_relationship(maya.id, sam.id, "married_to", source_id=src.id, directed=False)
    await store.set_attribute(maya.id, "city", "Tokyo", source_id=src.id)

    sl = await store.slice([maya.id])
    ids = {e.id for e in sl.subjects}
    assert maya.id in ids and sam.id in ids  # neighbour pulled in
    assert len(sl.relationships) == 1
    assert sl.attributes[maya.id][0].value == "Tokyo"


async def test_relationship_not_duplicated_on_restate(store):
    src = await store.add_source("user_stated")
    a = await store.add_subject("person", "Andy")
    b = await store.add_subject("pet", "Biscuit")
    await store.add_relationship(a.id, b.id, "owns", source_id=src.id)
    await store.add_relationship(a.id, b.id, "owns", source_id=src.id)  # restated
    rels = await store.relationships_for(a.id)
    assert len(rels) == 1


async def test_symmetric_relationship_dedup_reversed(store):
    src = await store.add_source("user_stated")
    a = await store.add_subject("person", "A")
    b = await store.add_subject("person", "B")
    await store.add_relationship(a.id, b.id, "friend_of", source_id=src.id, directed=False)
    # Same edge, stated the other way round.
    await store.add_relationship(b.id, a.id, "friend_of", source_id=src.id, directed=False)
    assert len(await store.relationships_for(a.id)) == 1


async def test_attribute_unchanged_value_no_churn(store):
    src = await store.add_source("user_stated")
    e = await store.add_subject("person", "Cy")
    await store.set_attribute(e.id, "city", "Tokyo", source_id=src.id)
    await store.set_attribute(e.id, "city", "Tokyo", source_id=src.id)  # same value
    assert len(await store.attributes_for(e.id, current_only=False)) == 1


async def test_merge_candidate_for_same_name_different_kind(store):
    await store.add_subject("person", "Sol")
    await store.add_subject("pet", "Sol")  # same name, different kind
    candidates = await store.open_merge_candidates()
    assert len(candidates) == 1


async def test_recall_finds_subject_and_message(store):
    await store.add_subject("person", "Maya Tanaka")
    await store.add_message("cli", "in", "Maya mentioned a trip to Portugal", speaker_id=None)

    hits = await store.recall("Maya")
    kinds = {h.kind for h in hits}
    assert "subject" in kinds
    assert "message" in kinds


async def test_message_sentiment_roundtrip(store):
    msg = await store.add_message(
        "telegram",
        "in",
        "ugh, rough day",
        sentiment="stressed",
        valence=-0.6,
        sentiment_confidence=0.7,
    )
    cur = await store.conn.execute("SELECT * FROM messages WHERE id = ?", (msg.id,))
    row = await cur.fetchone()
    assert row["sentiment"] == "stressed"
    assert row["valence"] == pytest.approx(-0.6)


async def test_thought_is_private_by_default(store):
    t = await store.add_thought("opinion", "I find this place a bit gloomy.")
    assert t.shareability == "private"
    assert t.prudence == 1.0


async def test_strongly_felt_thoughts_decay_slower(store):
    # The flashbulb effect: higher stability (from a strong valence) means a
    # thought keeps more of its prudence through a decay pass.
    felt = await store.add_thought("feeling", "deeply fond of this", valence=0.9, stability=0.9)
    flat = await store.add_thought("note", "a passing impression", stability=0.5)
    await store.decay_thoughts(rate=0.5, floor=0.0)
    cur = await store.conn.execute("SELECT id, prudence FROM thoughts")
    prudence = {r["id"]: r["prudence"] for r in await cur.fetchall()}
    assert prudence[felt.id] > prudence[flat.id]  # the felt thought stays prominent


async def test_thoughts_linked_to_subject(store):
    biscuit = await store.add_subject("pet", "Biscuit")
    other = await store.add_subject("person", "Mara")
    await store.add_thought("feeling", "fond of Biscuit", links=[("subject", biscuit.id)])
    await store.add_thought("opinion", "Mara seems kind", links=[("subject", other.id)])
    await store.add_thought("note", "unlinked musing")  # no link → not surfaced

    linked = await store.thoughts_linked_to(biscuit.id)
    assert [t.body for t in linked] == ["fond of Biscuit"]


async def test_thoughts_linked_to_ranks_by_prudence(store):
    e = await store.add_subject("place", "Kyoto")
    faded = await store.add_thought("opinion", "Kyoto was crowded", links=[("subject", e.id)])
    await store.add_thought("feeling", "Kyoto felt magical", links=[("subject", e.id)])
    # Fade one thought; the weightier (default prudence 1.0) should rank first.
    await store.decay_thoughts(rate=0.9, floor=0.1)
    fresh = await store.add_thought("feeling", "Kyoto stays with me", links=[("subject", e.id)])
    assert faded.id and fresh.id  # both recorded
    ranked = await store.thoughts_linked_to(e.id)
    assert ranked[0].prudence >= ranked[-1].prudence


async def test_thoughts_about_searches_bodies(store):
    await store.add_thought("opinion", "Lisbon felt warm and unhurried")
    await store.add_thought("feeling", "the trains were confusing")
    hits = await store.thoughts_about("Lisbon")
    assert [t.body for t in hits] == ["Lisbon felt warm and unhurried"]
    assert await store.thoughts_about("zzqq") == []


async def test_recent_thoughts_excludes_notes_and_faded(store):
    await store.add_thought("note", "housekeeping note")  # notes are not mood
    await store.add_thought("feeling", "quietly happy today")
    recent = await store.recent_thoughts()
    bodies = [t.body for t in recent]
    assert "quietly happy today" in bodies
    assert "housekeeping note" not in bodies


async def test_merge_carries_linked_thoughts_to_canonical(store):
    # A thought linked to a node that later gets folded into another must follow
    # the merge — Moko's inner life shouldn't dangle on the dead node.
    dup = await store.add_subject("person", "Bisc")
    canonical = await store.add_subject("pet", "Biscuit")
    await store.add_thought("feeling", "fond of this one", links=[("subject", dup.id)])

    await store.merge_subjects(dup.id, canonical.id, by="dream", reason="same pet")
    assert await store.thoughts_linked_to(dup.id) == []
    carried = await store.thoughts_linked_to(canonical.id)
    assert [t.body for t in carried] == ["fond of this one"]


async def test_merge_keeps_folded_names_recallable(store):
    # The point of keeping the folded node's name + aliases as aliases of the survivor is
    # that recall still finds it by those names. The merge must re-index the survivor —
    # otherwise _unindex(from_id) leaves the canonical subject unsearchable by the
    # duplicate's names, silently defeating the aliasing-on-merge promise.
    canon = await store.add_subject("person", "Maya Tanaka")
    dup = await store.add_subject("pet", "Whiskers")  # distinct name+kind → no auto-merge
    await store.add_alias(dup.id, "Fluffball")

    await store.merge_subjects(dup.id, canon.id, by="dream", reason="same")

    for term in ("Whiskers", "Fluffball"):  # the folded node's name AND its alias
        refs = {h.ref_id for h in await store.recall(term) if h.kind == "subject"}
        assert refs == {canon.id}, f"{term!r} should recall only the survivor, got {refs}"


async def test_session_lifecycle_and_rotation(store):
    first = await store.ensure_session()
    assert first.started_reason == "first"

    # ensure_session returns the same active session, not a new one.
    same = await store.ensure_session()
    assert same.id == first.id

    # Rotating ends the old session and opens a new active one.
    second = await store.start_session("dream")
    assert second.id != first.id
    assert second.started_reason == "dream"

    ended = await store.get_session(first.id)
    assert ended.ended_at is not None
    assert ended.ended_reason == "dream"

    active = await store.current_session()
    assert active.id == second.id


async def test_message_belongs_to_session(store):
    session = await store.ensure_session()
    msg = await store.add_message("cli", "in", "hello", session_id=session.id)
    assert msg.session_id == session.id

    await store.add_session_tokens(session.id, 42)
    refreshed = await store.get_session(session.id)
    assert refreshed.token_estimate == 42


async def test_commitments_due_and_complete(store):
    import datetime as dt

    from moko.clock import now_utc

    past = (now_utc() - dt.timedelta(hours=1)).isoformat()
    future = (now_utc() + dt.timedelta(days=1)).isoformat()

    due = await store.add_commitment("remind about the dentist", due_at=past)
    await store.add_commitment("send Mia the photos", due_at=future)
    await store.add_commitment("someday: learn the guitar")  # no due date

    # Only the past-due, open one surfaces.
    due_now = await store.due_commitments(now_utc().isoformat())
    assert [c.id for c in due_now] == [due.id]

    # All three are open.
    assert len(await store.open_commitments()) == 3

    # Completing it removes it from due + open.
    await store.complete_commitment(due.id)
    assert await store.due_commitments(now_utc().isoformat()) == []
    assert len(await store.open_commitments()) == 2


async def test_gap_lifecycle(store):
    maya = await store.add_subject("person", "Maya")
    gap = await store.add_gap(
        "When is Maya's birthday?", "missing_attr", about_id=maya.id, resolution_path="ask"
    )
    assert gap.status == "open"
    open_gaps = await store.open_gaps()
    assert any(g.id == gap.id for g in open_gaps)


async def test_resolving_a_gap_emits_a_feed_event(store):
    # Closing a gap surfaces a `gap.resolved` event (symmetric to gap.opened) so
    # the monitor's feed shows resolution, not a gap silently vanishing. Filter by
    # the gap's unique id — the bus is a process-wide singleton shared across tests.
    from moko.events import BUS

    gap = await store.add_gap("Is HeroQuest the reprint?", "open_curiosity")
    await store.set_gap_status(gap.id, "resolved")
    ev = next(
        (e for e in BUS.recent() if e.get("type") == "gap.resolved" and e.get("id") == gap.id),
        None,
    )
    assert ev is not None and ev["description"] == "Is HeroQuest the reprint?"


# ── §2.1: recall must not surface a superseded/stale fact ──────────────────────
async def test_recall_drops_a_superseded_attribute_value(store):
    e = await store.add_subject("person", "Maya")
    src = await store.add_source("user_stated")
    await store.set_attribute(e.id, "city", "Boston", source_id=src.id)
    assert any("Boston" in h.content for h in await store.recall("Boston"))
    await store.set_attribute(e.id, "city", "NYC", source_id=src.id)  # corrected
    contents = " ".join(h.content for h in await store.recall("Boston city NYC"))
    assert "NYC" in contents and "Boston" not in contents  # only the current value


async def test_end_attribute_removes_it_from_recall(store):
    e = await store.add_subject("place", "Cafe")
    src = await store.add_source("user_stated")
    attr = await store.set_attribute(e.id, "hours", "9to5daily", source_id=src.id)
    assert any("9to5daily" in h.content for h in await store.recall("9to5daily"))
    await store.end_attribute(attr.id)
    assert not any("9to5daily" in h.content for h in await store.recall("9to5daily"))


# ── §3.5: typed values are validated on write ─────────────────────────────────
async def test_value_type_is_validated_on_write(store):
    e = await store.add_subject("person", "Ada")
    src = await store.add_source("user_stated")
    # a value that doesn't parse as the declared type → stored as a string (kept)
    bad = await store.set_attribute(
        e.id, "age", "around thirty", source_id=src.id, value_type="number"
    )
    assert bad.value_type == "string" and bad.value == "around thirty"
    # values that DO parse keep their declared type
    born = await store.set_attribute(
        e.id, "born", "1990-12-10", source_id=src.id, value_type="date"
    )
    assert born.value_type == "date"
    num = await store.set_attribute(e.id, "h_cm", "170", source_id=src.id, value_type="number")
    assert num.value_type == "number"
    # an unknown type label falls back to string
    weird = await store.set_attribute(e.id, "z", "y", source_id=src.id, value_type="num")
    assert weird.value_type == "string"


# ── feature: auto-snapshot the mind before a migration changes its schema ──────
async def test_pre_migration_snapshot_protects_existing_data(tmp_path, monkeypatch):
    import aiosqlite

    import moko.store.db as db

    dbfile = tmp_path / "moko.db"
    real = db._migration_files()  # the real, already-applied set

    async def _migrate(path):
        conn = await aiosqlite.connect(path)
        conn.row_factory = aiosqlite.Row
        await db._migrate(conn, str(path))
        await conn.close()

    # 1) fresh init with the real migrations — nothing to lose, so NO snapshot.
    await _migrate(dbfile)
    snap_dir = tmp_path / "snapshots"
    assert not snap_dir.exists()

    # 2) a NEW migration arrives → the populated DB is backed up before applying it.
    extra = tmp_path / "0999_extra.sql"
    extra.write_text("CREATE TABLE extra_probe (x);")
    monkeypatch.setattr(db, "_migration_files", lambda: [*real, extra])
    await _migrate(dbfile)
    snaps = list(snap_dir.glob("*.db"))
    assert len(snaps) == 1 and snaps[0].name == "pre-v999.db"  # backed up before migrating


# ── §2.2: a merge re-points EVERY subject FK, not just a subset ─────────────────
async def test_merge_repoints_messages_gaps_and_commitments(store):
    src = await store.add_source("user_stated")
    dup = await store.add_subject("person", "Bob")
    canon = await store.add_subject("person", "Robert")
    msg = await store.add_message("cli", "in", "hi from Bob", speaker_id=dup.id)
    gap = await store.add_gap("Bob's birthday?", "missing_attr", about_id=dup.id)
    com = await store.add_commitment("call Bob", about_id=dup.id)
    obs = await store.add_observation(
        dup.id, "sleep_hours", "7", source_id=src.id, observed_at="2026-06-27T07:00:00+00:00"
    )

    await store.merge_subjects(dup.id, canon.id, by="dream", reason="same person")

    async def _col(table, col, rid):
        cur = await store.conn.execute(f"SELECT {col} FROM {table} WHERE id=?", (rid,))
        return (await cur.fetchone())[col]

    # history, open curiosity, held commitment, and the observation series all follow
    # the fold — nothing orphans on the dead node.
    assert await _col("messages", "speaker_id", msg.id) == canon.id
    assert await _col("knowledge_gaps", "about_id", gap.id) == canon.id
    assert await _col("commitments", "about_id", com.id) == canon.id
    assert await _col("observations", "subject_id", obs.id) == canon.id


# ── §2.3: a migration is all-or-nothing ───────────────────────────────────────
async def test_migration_is_atomic_on_failure(tmp_path, monkeypatch):
    import aiosqlite

    import moko.store.db as db

    bad = tmp_path / "0999_bad.sql"
    bad.write_text("CREATE TABLE atomic_probe (x);\nINSERT INTO nope_no_such_table VALUES (1);")
    real = db._migration_files()
    monkeypatch.setattr(db, "_migration_files", lambda: [*real, bad])

    conn = await aiosqlite.connect(tmp_path / "m.db")
    conn.row_factory = aiosqlite.Row
    try:
        with pytest.raises(Exception):  # the bad INSERT aborts the script
            await db._migrate(conn)
        # the partial DDL was rolled back …
        cur = await conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='atomic_probe'"
        )
        assert await cur.fetchone() is None
        # … and the version wasn't recorded, so the next boot re-runs cleanly.
        cur = await conn.execute("SELECT version FROM schema_migrations WHERE version=999")
        assert await cur.fetchone() is None
    finally:
        await conn.close()


async def test_observation_series_accumulates(store):
    """Two readings under one key on different days both survive — an observation
    never supersedes (the opposite of an attribute)."""
    src = await store.add_source("user_stated")
    me = await store.add_subject("person", "Andrew")
    await store.add_observation(
        me.id, "sleep_hours", "7.2", source_id=src.id,
        value_type="number", observed_at="2026-06-27T07:00:00+00:00",
    )
    await store.add_observation(
        me.id, "sleep_hours", "6.0", source_id=src.id,
        value_type="number", observed_at="2026-06-28T07:00:00+00:00",
    )
    series = await store.observations_for(me.id, key="sleep_hours")
    assert [o.value for o in series] == ["6.0", "7.2"]  # newest first, both kept


async def test_observation_restate_same_instant_updates_not_duplicates(store):
    """Restating the same (subject, key, observed_at) updates in place, so a
    COUNT/AVG over the series can't double-count."""
    src = await store.add_source("user_stated")
    me = await store.add_subject("person", "Andrew")
    when = "2026-06-28T07:00:00+00:00"
    await store.add_observation(me.id, "sleep_hours", "6.0", source_id=src.id, observed_at=when)
    await store.add_observation(me.id, "sleep_hours", "6.5", source_id=src.id, observed_at=when)
    series = await store.observations_for(me.id, key="sleep_hours")
    assert len(series) == 1
    assert series[0].value == "6.5"


async def test_observation_requires_source(store):
    me = await store.add_subject("person", "Andrew")
    with pytest.raises(ValueError):
        await store.add_observation(me.id, "sleep_hours", "6", source_id="")


async def test_latest_observations_one_per_series(store):
    src = await store.add_source("user_stated")
    me = await store.add_subject("person", "Andrew")
    await store.add_observation(
        me.id, "sleep_hours", "7", source_id=src.id, observed_at="2026-06-27T07:00:00+00:00"
    )
    await store.add_observation(
        me.id, "sleep_hours", "6", source_id=src.id, observed_at="2026-06-28T07:00:00+00:00"
    )
    await store.add_observation(
        me.id, "weight_kg", "70", source_id=src.id, observed_at="2026-06-28T07:00:00+00:00"
    )
    latest = {o.key: o.value for o in await store.latest_observations(me.id)}
    assert latest == {"sleep_hours": "6", "weight_kg": "70"}


# ── recall ranks by relevance, not insertion order ────────────────────────────
async def test_recall_ranks_best_match_first(store):
    """FTS5 defaults to rowid (insertion) order; without ORDER BY bm25 a common term
    surfaces the *oldest* matches as the mind grows. The most-relevant row (the term
    twice) must rank above older rows that mention it once."""
    # Older rows that mention "garden" once — inserted first (lower rowid).
    for i in range(5):
        await store.add_message("cli", "in", f"a walk in the garden, note {i}", speaker_id=None)
    # A newer, denser match — "garden" appears twice, so bm25 ranks it best.
    await store.add_message("cli", "in", "garden garden — the greenhouse", speaker_id=None)

    hits = await store.recall("garden", limit=3)
    assert hits, "the term should match"
    assert "greenhouse" in hits[0].content  # densest match first, not the oldest


# ── decay_thoughts leaves floored rows untouched ──────────────────────────────
async def test_decay_thoughts_skips_floored_rows(store):
    """The nightly decay must not rewrite already-floored rows (WHERE prudence > floor)
    — they're fixpoints. It returns only the count of rows it actually moved."""
    floored = await store.add_thought("note", "already faded", prudence=0.1, stability=0.5)
    live = await store.add_thought("opinion", "still weighty", prudence=1.0, stability=0.5)

    touched = await store.decay_thoughts(rate=0.5, floor=0.1)
    assert touched == 1  # only the live row moved; the floored one was skipped

    cur = await store.conn.execute("SELECT id, prudence, last_recompute_at FROM thoughts")
    rows = {r["id"]: r for r in await cur.fetchall()}
    # the floored row is untouched: value unchanged and never stamped
    assert rows[floored.id]["prudence"] == pytest.approx(0.1)
    assert rows[floored.id]["last_recompute_at"] is None
    # the live row decayed toward (but not below) the floor
    assert rows[live.id]["prudence"] < 1.0
    assert rows[live.id]["prudence"] >= 0.1


# ── a read-only handle reads but cannot write ─────────────────────────────────
async def test_open_readonly_reads_but_write_raises(tmp_path):
    """The monitor observes the mind through a read-only handle — it reads what the
    writer stored, but any write raises rather than corrupting the record."""
    import aiosqlite

    path = tmp_path / "moko.db"
    writer = await Store.open(path)
    try:
        maya = await writer.add_subject("person", "Maya")
    finally:
        await writer.close()

    ro = await Store.open_readonly(path)
    try:
        # it reads the writer's data …
        got = await ro.get_subject(maya.id)
        assert got is not None and got.name == "Maya"
        # … but a write is refused by the read-only connection.
        with pytest.raises(aiosqlite.OperationalError):
            await ro.conn.execute(
                "INSERT INTO subjects (id, kind, name, confidence, notes, created_at, world_id)"
                " VALUES ('x', 'person', 'Nope', 1.0, NULL, '2026-01-01T00:00:00+00:00',"
                " 'world-companion')"
            )
    finally:
        await ro.close()


# ── heal_search removes a superseded attribute's stale FTS row ────────────────
async def test_heal_search_removes_superseded_attribute_row(store):
    """A supersede path that skipped `_unindex` leaks a search row for a no-longer-true
    fact — the exact class `integrity._stale_in_recall` monitors. heal_search deletes it."""
    from moko.store import integrity

    src = await store.add_source("user_stated")
    e = await store.add_subject("person", "Dana")
    attr = await store.set_attribute(e.id, "city", "Boston", source_id=src.id)

    # Simulate a leak: supersede the attribute in the graph but leave its FTS row behind
    # (as a buggy supersede path would), so recall could still surface the stale value.
    await store.conn.execute(
        "UPDATE attributes SET valid_to = ? WHERE id = ?",
        ("2026-01-01T00:00:00+00:00", attr.id),
    )
    await store.conn.commit()
    stale = await integrity._stale_in_recall(store.conn)
    assert stale.count == 1  # the leak is present …

    healed = await store.heal_search()
    assert healed == 1

    cur = await store.conn.execute("SELECT count(*) AS n FROM search WHERE ref_id = ?", (attr.id,))
    assert (await cur.fetchone())["n"] == 0  # … and its search row is gone
    assert (await integrity._stale_in_recall(store.conn)).count == 0


async def test_add_past_attribute_slots_into_history_and_dedups(store):
    # "She used to be a receptionist" (before nurse, now doctor) records under the SAME key as
    # history — never a parallel previous_X key — slotted before the current chain, and a retry
    # is deduped. (The reported bug turned one past value into previous_occupation + _2.)
    src = await store.add_source("user_stated")
    maya = await store.add_subject("person", "Maya")
    await store.set_attribute(maya.id, "occupation", "nurse", source_id=src.id)
    await store.set_attribute(maya.id, "occupation", "doctor", source_id=src.id)
    await store.add_past_attribute(maya.id, "occupation", "receptionist", source_id=src.id)
    await store.add_past_attribute(maya.id, "occupation", "receptionist", source_id=src.id)  # retry

    occ = [a for a in await store.attributes_for(maya.id, current_only=False)
           if a.key == "occupation"]
    assert {a.value for a in occ} == {"receptionist", "nurse", "doctor"}  # one key, full timeline
    assert len(occ) == 3  # receptionist recorded once — the retry deduped
    current = [a for a in occ if a.valid_to is None]
    assert len(current) == 1 and current[0].value == "doctor"  # current value untouched
    receptionist = next(a for a in occ if a.value == "receptionist")
    nurse = next(a for a in occ if a.value == "nurse")
    assert receptionist.valid_from is None  # start unknown
    assert receptionist.valid_to is not None  # ended (a past value)
    assert receptionist.valid_to <= nurse.valid_to  # slots before nurse in the timeline
    all_keys = {a.key for a in await store.attributes_for(maya.id, current_only=False)}
    assert not any(k.startswith(("previous_", "former_", "old_")) for k in all_keys)  # no parallel
