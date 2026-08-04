"""The monitor: store→bus wiring, the status snapshot, and the HTTP API."""

import asyncio
import contextlib

import pytest
from aiohttp.test_utils import TestClient, TestServer

from moko import events
from moko.config.config import Config
from moko.monitor.server import SPA_KEY, build_app, build_status
from moko.store import Store

CFG = Config(
    companion_name="Jordan",
    timezone="Asia/Tokyo",
    sleep_start="23:00",
    sleep_end="07:00",
    proactivity="moderate",
    llm_provider="anthropic",
    llm_model="claude-opus-4-8",
)


@pytest.fixture
async def store(tmp_path):
    s = await Store.open(tmp_path / "moko.db")
    try:
        yield s
    finally:
        await s.close()


async def test_add_message_publishes_to_bus(store):
    sub = events.subscribe(backfill=False)
    try:
        await store.add_message("cli", "in", "hello there")
        ev = await asyncio.wait_for(sub.queue.get(), timeout=1)
        assert ev["type"] == "message"
        assert ev["direction"] == "in"
        assert ev["body"] == "hello there"
    finally:
        sub.close()


async def test_add_gap_publishes_to_bus(store):
    sub = events.subscribe(backfill=False)
    try:
        await store.add_gap("Where did Sam grow up?", "fact", resolution_path="ask")
        ev = await asyncio.wait_for(sub.queue.get(), timeout=1)
        assert ev["type"] == "gap.opened"
        assert ev["resolution_path"] == "ask"
        assert "Sam" in ev["description"]
    finally:
        sub.close()


async def test_add_thought_publishes_to_bus(store):
    sub = events.subscribe(backfill=False)
    try:
        await store.add_thought("impression", "they seem tired", shareability="private")
        ev = await asyncio.wait_for(sub.queue.get(), timeout=1)
        assert ev["type"] == "thought"
        assert ev["shareability"] == "private"
        assert ev["body"] == "they seem tired"
    finally:
        sub.close()


async def test_build_status_shape(store):
    status = await build_status(store, CFG)
    assert status["companion"] == "Jordan"
    assert status["llm"]["model"] == "claude-opus-4-8"
    assert "moko_version" in status
    assert "python" in status
    assert isinstance(status["observers"], int)
    # counts come from the store and cover the mind's tables
    assert status["counts"]["messages"] == 0
    assert "thoughts" in status["counts"]


async def test_build_status_service_state_runs_off_the_loop(store, monkeypatch):
    # service_state() shells out to systemctl (blocking). build_status must run it via
    # asyncio.to_thread so a slow/hung systemctl can't stall the agent's single loop.
    # Proof: while a *blocking* service_state sleeps, a loop heartbeat keeps ticking.
    import time

    from moko.cli import service

    def slow_blocking_state() -> dict:
        time.sleep(0.3)  # a synchronous stall — fatal on the loop, harmless off it
        return {"active": True}

    monkeypatch.setattr(service, "service_state", slow_blocking_state)

    ticks = 0

    async def heartbeat() -> None:
        nonlocal ticks
        while True:
            ticks += 1
            await asyncio.sleep(0.01)

    beat = asyncio.create_task(heartbeat())
    try:
        status = await build_status(store, CFG)
    finally:
        beat.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await beat

    assert status["service"] == {"active": True}  # the off-thread result still lands
    # If service_state ran ON the loop, the 0.3s sleep would freeze the heartbeat entirely.
    # Off-thread, the loop stays live and the heartbeat ticks many times during that window.
    assert ticks > 5, f"loop appears to have blocked (only {ticks} ticks)"


async def test_rhythms_endpoint(store):
    client = TestClient(TestServer(build_app(store, CFG)))
    await client.start_server()
    try:
        resp = await client.get("/api/rhythms")
        assert resp.status == 200
        data = await resp.json()
        names = {r["name"] for r in data}
        assert names == {"settle", "reflection", "sleeping", "outreach", "research"}
        assert all("state" in r and "detail" in r for r in data)
    finally:
        await client.close()


async def test_status_endpoint(store):
    client = TestClient(TestServer(build_app(store, CFG)))
    await client.start_server()
    try:
        resp = await client.get("/api/status")
        assert resp.status == 200
        data = await resp.json()
        assert data["companion"] == "Jordan"
        assert data["counts"]["messages"] == 0
    finally:
        await client.close()


async def test_brain_thoughts_paginate(store):
    # Twelve thoughts; a small page size proves limit+offset page through them
    # newest-first with no overlap, and that a bad ?limit= falls back safely.
    for i in range(12):
        await store.add_thought("note", f"thought {i:02d}")
    client = TestClient(TestServer(build_app(store, CFG)))
    await client.start_server()
    try:
        p0 = await (await client.get("/api/brain/thoughts?limit=5&offset=0")).json()
        p1 = await (await client.get("/api/brain/thoughts?limit=5&offset=5")).json()
        assert len(p0) == 5 and len(p1) == 5
        assert p0[0]["body"] == "thought 11"  # newest first
        ids = {t["id"] for t in p0} | {t["id"] for t in p1}
        assert len(ids) == 10  # pages don't overlap
        # A garbage limit is ignored — the serializer's default page still serves.
        junk = await (await client.get("/api/brain/thoughts?limit=nope")).json()
        assert len(junk) == 12
    finally:
        await client.close()


async def test_brain_commitments_include_done(store):
    # The browser shows the full commitment history, newest-first — a completed
    # one stays visible (the badge counts all rows, so the list must too).
    a = await store.add_commitment("water the plants", kind="task")
    await store.add_commitment("call Dad", kind="task")
    await store.complete_commitment(a.id)
    client = TestClient(TestServer(build_app(store, CFG)))
    await client.start_server()
    try:
        data = await (await client.get("/api/brain/commitments")).json()
        assert len(data) == 2  # both, not just the open one
        assert data[0]["description"] == "call Dad"  # newest first
        statuses = {c["description"]: c["status"] for c in data}
        assert statuses["water the plants"] == "done"
    finally:
        await client.close()


async def test_usage_endpoint_reports_tokens_and_media(store):
    # The Token Tracker: per-call rows, roll-ups by activity, and generated-media counts.
    await store.usage.add_llm_call(kind="reply", model="m", input_tokens=100, output_tokens=50)
    await store.usage.add_llm_call(kind="dream", model="m", input_tokens=200, output_tokens=20)
    await store.add_media(b"x", kind="image", direction="out", source_kind="inference")

    client = TestClient(TestServer(build_app(store, CFG)))
    await client.start_server()
    try:
        data = await (await client.get("/api/usage")).json()
        assert len(data["rows"]) == 2
        assert data["all_time"]["totals"]["calls"] == 2
        assert data["all_time"]["totals"]["input_tokens"] == 300
        assert data["all_time"]["by_kind"][0]["kind"] == "dream"  # biggest spender first
        assert data["media"] == {"image": 1}
        # Both calls landed inside the rolling windows.
        assert data["window_24h"]["totals"]["calls"] == 2
    finally:
        await client.close()


async def test_subjects_endpoint_filters_by_world(store):
    # The Brain → Subjects view can scope to the companion's world or Moko's own (M11).
    from moko.store import COMPANION_WORLD, MOKO_WORLD

    await store.add_subject("person", "Priya", world_id=COMPANION_WORLD)
    await store.add_subject("place", "the den", world_id=MOKO_WORLD)
    client = TestClient(TestServer(build_app(store, CFG)))
    await client.start_server()
    try:
        allw = await (await client.get("/api/brain/subjects")).json()
        assert {"Priya", "the den"} <= {s["name"] for s in allw}  # both, unfiltered
        comp = await (await client.get("/api/brain/subjects?world=companion")).json()
        assert {s["name"] for s in comp} == {"Priya"}
        moko = await (await client.get("/api/brain/subjects?world=moko")).json()
        assert {s["name"] for s in moko} == {"the den"}
        assert moko[0]["world_id"] == MOKO_WORLD  # rides on each row for the badge
    finally:
        await client.close()


async def test_subjects_count_matches_list(store):
    # The Subjects tab badge must equal the rows the list shows: both exclude Moko's
    # own `self` node and merged-away duplicates. (Symptom: badge 2, list 1 — the
    # hidden one being Moko's self-node in its own world.)
    from moko.store import COMPANION_WORLD, MOKO_WORLD

    await store.add_subject("person", "Priya", world_id=COMPANION_WORLD)
    await store.add_subject("self", "Moko", world_id=MOKO_WORLD)  # not a listed subject
    dup = await store.add_subject("person", "Priya P.", world_id=COMPANION_WORLD)
    keep = await store.add_subject("person", "Priya Patel", world_id=COMPANION_WORLD)
    await store.merge_subjects(dup.id, keep.id, by="test", reason="same person")  # merged away

    client = TestClient(TestServer(build_app(store, CFG)))
    await client.start_server()
    try:
        status = await (await client.get("/api/status")).json()
        listed = await (await client.get("/api/brain/subjects")).json()
        # Two live, non-self subjects survive (Priya + Priya Patel); the self node and
        # the merged-away dup are in neither the badge nor the list.
        assert status["counts"]["subjects"] == len(listed) == 2
        assert "Moko" not in {s["name"] for s in listed}
    finally:
        await client.close()


async def test_learned_events_for_subject_fact_and_link(store):
    src = await store.add_source("test")

    sub = events.subscribe(backfill=False)
    try:
        s = await store.add_subject("person", "Priya")
        ev = await asyncio.wait_for(sub.queue.get(), timeout=1)
        assert ev["type"] == "learned" and ev["what"] == "subject"
        assert ev["name"] == "Priya" and ev["kind"] == "person"
    finally:
        sub.close()

    sub = events.subscribe(backfill=False)
    try:
        await store.set_attribute(s.id, "city", "Tokyo", source_id=src.id)
        ev = await asyncio.wait_for(sub.queue.get(), timeout=1)
        assert ev["type"] == "learned" and ev["what"] == "fact" and ev["key"] == "city"
    finally:
        sub.close()

    other = await store.add_subject("person", "Sam")
    sub = events.subscribe(backfill=False)
    try:
        await store.add_relationship(s.id, other.id, "knows", source_id=src.id)
        ev = await asyncio.wait_for(sub.queue.get(), timeout=1)
        assert ev["type"] == "learned" and ev["what"] == "link" and ev["rel_type"] == "knows"
    finally:
        sub.close()


async def test_learned_events_are_live_only_not_backfilled(store):
    # learned events stream live, but a burst (world-authoring / a dream) must NOT cycle the
    # backfill ring and evict the narrative a just-opened monitor replays.
    await store.add_message("cli", "in", "hi there")  # narrative → backfilled
    for i in range(40):  # a learned burst
        await store.add_subject("topic", f"thing{i}")
    ring_types = [e["type"] for e in events.BUS.recent()]
    assert "message" in ring_types  # the narrative survived the burst
    assert "learned" not in ring_types  # learned never enters the backfill ring


async def test_commitment_events_publish(store):
    sub = events.subscribe(backfill=False)
    try:
        c = await store.add_commitment("water the plants", kind="task")
        ev = await asyncio.wait_for(sub.queue.get(), timeout=1)
        assert ev["type"] == "commitment.created" and ev["description"] == "water the plants"
    finally:
        sub.close()
    sub = events.subscribe(backfill=False)
    try:
        await store.complete_commitment(c.id)
        ev = await asyncio.wait_for(sub.queue.get(), timeout=1)
        assert ev["type"] == "commitment.completed" and ev["description"] == "water the plants"
    finally:
        sub.close()


async def test_brain_thoughts_and_gaps_filter_by_world(store):
    from moko.store import MOKO_WORLD

    await store.add_thought("feeling", "Andrew seemed tired")  # companion (default)
    await store.add_thought("feeling", "my kitchen is cozy", world_id=MOKO_WORLD)
    await store.add_gap("who is Priya?", "identity")  # companion
    await store.add_gap("what's in the attic?", "exploration", world_id=MOKO_WORLD)
    client = TestClient(TestServer(build_app(store, CFG)))
    await client.start_server()
    try:
        moko_thoughts = await (await client.get("/api/brain/thoughts?world=moko")).json()
        assert [t["body"] for t in moko_thoughts] == ["my kitchen is cozy"]
        comp_gaps = await (await client.get("/api/brain/gaps?world=companion")).json()
        assert [g["description"] for g in comp_gaps] == ["who is Priya?"]
        assert len(await (await client.get("/api/brain/thoughts")).json()) == 2  # unscoped
    finally:
        await client.close()


async def test_usage_detail_endpoint_returns_full_io(store):
    rid = await store.usage.add_llm_call(
        kind="reply", model="m", input_tokens=12, output_tokens=3,
        system_prompt="you are Moko", request='[{"role":"user","text":"hi"}]', response="hello!",
    )
    client = TestClient(TestServer(build_app(store, CFG)))
    await client.start_server()
    try:
        d = await (await client.get(f"/api/usage/{rid}")).json()
        assert d["system_prompt"] == "you are Moko" and d["response"] == "hello!"
        assert "hi" in d["request"]
        assert (await client.get("/api/usage/nope")).status == 404
    finally:
        await client.close()


async def test_media_endpoints_list_and_serve_bytes(store):
    # The Media gallery: a metadata list + a by-id blob server (path taken from the row,
    # never the request).
    from moko.config.paths import ensure_home

    ensure_home()
    art = await store.add_media(b"PNGDATA", kind="image", mime="image/png", filename="moko.png",
                                caption="a cozy room", description="a self-portrait",
                                direction="out", source_kind="inference")
    client = TestClient(TestServer(build_app(store, CFG)))
    await client.start_server()
    try:
        row = next(m for m in await (await client.get("/api/media")).json() if m["id"] == art.id)
        # The list carries the full shape the gallery renders (incl. the world it belongs to).
        assert row["kind"] == "image" and row["mime"] == "image/png"
        assert row["caption"] == "a cozy room" and row["description"] == "a self-portrait"
        assert row["direction"] == "out" and row["filename"] == "moko.png" and row["created_at"]
        assert row["world_id"] == "world-companion"  # default world tag rides on each row

        blob = await client.get(f"/api/media/blob/{art.id}")
        assert blob.status == 200 and await blob.read() == b"PNGDATA"
        assert blob.headers["Content-Type"].startswith("image/png")
        assert blob.headers["X-Content-Type-Options"] == "nosniff"  # never sniff a stored mime
        assert blob.headers["Content-Disposition"].startswith("inline")  # an image displays
        assert (await client.get("/api/media/blob/nope")).status == 404  # unknown id
    finally:
        await client.close()


async def test_media_blob_serves_a_thumbnail(store):
    # ?thumb=1 returns a small cached JPEG (so the grid doesn't ship full images).
    import io

    from PIL import Image

    from moko.config.paths import ensure_home

    ensure_home()
    buf = io.BytesIO()
    Image.new("RGB", (1024, 1536), (200, 120, 170)).save(buf, "PNG")  # a portrait "photo"
    art = await store.add_media(buf.getvalue(), kind="image", mime="image/png",
                                direction="out", source_kind="inference")
    client = TestClient(TestServer(build_app(store, CFG)))
    await client.start_server()
    try:
        full = await client.get(f"/api/media/blob/{art.id}")
        thumb = await client.get(f"/api/media/blob/{art.id}?thumb=1")
        assert thumb.status == 200
        assert thumb.headers["Content-Type"] == "image/jpeg"
        full_bytes, thumb_bytes = await full.read(), await thumb.read()
        assert len(thumb_bytes) < len(full_bytes)  # genuinely smaller
        assert Image.open(io.BytesIO(thumb_bytes)).size[0] <= 320  # downscaled
    finally:
        await client.close()


async def test_media_blob_refuses_a_path_outside_the_media_root(store):
    # Defence in depth: even if a row's stored path somehow escaped ~/.moko/media, refuse it.
    from moko.config.paths import ensure_home

    ensure_home()
    art = await store.add_media(b"x", kind="image", mime="image/png",
                                direction="out", source_kind="inference")
    await store.conn.execute("UPDATE media SET path = ? WHERE id = ?",
                             ("../../../../etc/passwd", art.id))
    await store.conn.commit()
    client = TestClient(TestServer(build_app(store, CFG)))
    await client.start_server()
    try:
        assert (await client.get(f"/api/media/blob/{art.id}")).status == 403
    finally:
        await client.close()


async def test_media_blob_404_when_the_file_is_gone(store):
    # A present row whose blob was removed degrades to 404, never a 500.
    from moko.config.paths import ensure_home, media_dir

    ensure_home()
    art = await store.add_media(b"x", kind="image", mime="image/png",
                                direction="out", source_kind="inference")
    (media_dir() / art.path).unlink()
    client = TestClient(TestServer(build_app(store, CFG)))
    await client.start_server()
    try:
        assert (await client.get(f"/api/media/blob/{art.id}")).status == 404
    finally:
        await client.close()


async def test_media_list_paginates(store):
    from moko.config.paths import ensure_home

    ensure_home()
    for i in range(12):
        await store.add_media(f"img{i}".encode(), kind="image", mime="image/png",
                              direction="out", source_kind="inference")
    client = TestClient(TestServer(build_app(store, CFG)))
    await client.start_server()
    try:
        p0 = await (await client.get("/api/media?limit=5&offset=0")).json()
        p1 = await (await client.get("/api/media?limit=5&offset=5")).json()
        assert len(p0) == 5 and len(p1) == 5
        assert not ({m["id"] for m in p0} & {m["id"] for m in p1})  # no overlap across pages
    finally:
        await client.close()


def test_monitor_port_has_one_source():
    # The default monitor port lives in exactly one place: monitor.server.DEFAULT_PORT.
    # access.MONITOR_PORT must derive from it, not carry its own duplicated literal (which
    # could silently drift). config.runtime.monitor_port is the runtime override — separate.
    from moko.monitor import access
    from moko.monitor.server import DEFAULT_PORT

    assert access.MONITOR_PORT == DEFAULT_PORT
    # reachable_url / lan_firewall_command default to that single source, too.
    assert f":{DEFAULT_PORT}" in access.reachable_url("local")
    assert f"port {DEFAULT_PORT} " in access.lan_firewall_command()


async def test_monitor_is_served_a_read_only_store(tmp_path):
    # The monitor gets its OWN read-only connection (Store.open_readonly), so it's a
    # structural pure observer — it can read the mind but never write it. The read-write
    # Store stays with the gateway's serial consumer.
    db = tmp_path / "moko.db"
    rw = await Store.open(db)  # the writer must create the DB + WAL first
    try:
        await rw.add_message("cli", "in", "hello")  # something to read back
        ro = await Store.open_readonly(db)
        try:
            app = build_app(ro, CFG)
            client = TestClient(TestServer(app))
            await client.start_server()
            try:
                # Reads work through the read-only handle.
                data = await (await client.get("/api/status")).json()
                assert data["counts"]["messages"] == 1
            finally:
                await client.close()
            # And the handle genuinely refuses writes — the "read-only" is real, not asserted.
            import aiosqlite

            with pytest.raises(aiosqlite.OperationalError):
                await ro.add_message("cli", "in", "the monitor must not be able to write this")
        finally:
            await ro.close()
    finally:
        await rw.close()


async def test_unbuilt_spa_serves_hint_page(store):
    # With no built SPA on disk, the catch-all returns the friendly hint, not a 500.
    app = build_app(store, CFG)
    app[SPA_KEY] = None  # force the unbuilt path regardless of environment
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        resp = await client.get("/")
        assert resp.status == 200
        body = await resp.text()
        assert "Moko monitor" in body
    finally:
        await client.close()
