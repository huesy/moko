"""The brain browser's read-only API over a populated mind."""

import pytest
from aiohttp.test_utils import TestClient, TestServer

from moko.config.config import Config
from moko.monitor.server import build_app
from moko.store import Store

CFG = Config(
    companion_name="Jordan", timezone="UTC", sleep_start="23:00",
    sleep_end="07:00", proactivity="moderate",
)


@pytest.fixture
async def populated(tmp_path):
    s = await Store.open(tmp_path / "moko.db")
    src = await s.add_source("test")
    maya = await s.add_subject("person", "Maya")
    sam = await s.add_subject("person", "Sam")
    await s.set_attribute(maya.id, "role", "engineer", source_id=src.id)
    await s.add_relationship(maya.id, sam.id, "married_to", source_id=src.id)
    await s.add_thought("impression", "Maya seems thoughtful", links=[("subject", maya.id)])
    await s.add_gap("What does Sam do?", "fact", about_id=sam.id)
    await s.add_commitment("Ask Maya about the trip")
    await s.add_message("cli", "in", "hi Moko")
    try:
        yield s, maya.id
    finally:
        await s.close()


async def _client(store):
    client = TestClient(TestServer(build_app(store, CFG)))
    await client.start_server()
    return client


@pytest.mark.parametrize(
    "path,expect",
    [
        ("/api/brain/thoughts", "Maya seems thoughtful"),
        ("/api/brain/gaps", "What does Sam do?"),
        ("/api/brain/commitments", "Ask Maya about the trip"),
        ("/api/brain/messages", "hi Moko"),
        ("/api/brain/subjects", "Maya"),
    ],
)
async def test_brain_list_endpoints(populated, path, expect):
    store, _ = populated
    client = await _client(store)
    try:
        resp = await client.get(path)
        assert resp.status == 200
        body = await resp.text()
        assert expect in body
    finally:
        await client.close()


async def test_subject_detail_includes_attrs_rels_thoughts(populated):
    store, maya_id = populated
    client = await _client(store)
    try:
        resp = await client.get(f"/api/brain/subject/{maya_id}")
        assert resp.status == 200
        data = await resp.json()
        assert data["subject"]["name"] == "Maya"
        assert any(a["key"] == "role" and a["value"] == "engineer" for a in data["attributes"])
        assert any(r["type"] == "married_to" and r["other_name"] == "Sam"
                   for r in data["relationships"])
        assert any("thoughtful" in t["body"] for t in data["thoughts"])
    finally:
        await client.close()


async def test_subject_detail_404_for_unknown(populated):
    store, _ = populated
    client = await _client(store)
    try:
        resp = await client.get("/api/brain/subject/nope")
        assert resp.status == 404
    finally:
        await client.close()


async def test_subject_detail_includes_gaps_about_it(populated):
    # The subject view now surfaces the curiosities about that subject, alongside
    # its thoughts — so a subject's open questions are visible on its card.
    store, _ = populated
    sam = await store.find_by_name("Sam")
    client = await _client(store)
    try:
        resp = await client.get(f"/api/brain/subject/{sam.id}")
        data = await resp.json()
        assert any("What does Sam do?" in g["description"] for g in data["gaps"])
    finally:
        await client.close()


async def test_thought_detail_surfaces_same_subject_gaps(populated):
    # The link the companion asked for: a thought about Maya and a curiosity about
    # Maya are navigable to each other (mediated by the shared subject subject).
    store, maya_id = populated
    await store.add_gap("Where does Maya live?", "missing_attr", about_id=maya_id)
    tid = (await store.thoughts_linked_to(maya_id))[0].id
    client = await _client(store)
    try:
        resp = await client.get(f"/api/brain/thought/{tid}")
        assert resp.status == 200
        data = await resp.json()
        assert data["thought"]["body"] == "Maya seems thoughtful"
        assert any(e["name"] == "Maya" for e in data["subjects"])  # navigable to subject
        assert any("Where does Maya live?" in g["description"] for g in data["gaps"])  # the link
    finally:
        await client.close()


async def test_gap_detail_surfaces_subject_and_thoughts(populated):
    # The inverse: clicking a curiosity surfaces its subject and the thoughts about
    # that subject — thought↔gap navigation in both directions.
    store, _ = populated
    sam = await store.find_by_name("Sam")
    await store.add_thought("hypothesis", "Sam works nights", links=[("subject", sam.id)])
    gid = (await store.gaps_about(sam.id))[0].id
    client = await _client(store)
    try:
        resp = await client.get(f"/api/brain/gap/{gid}")
        assert resp.status == 200
        data = await resp.json()
        assert data["subject"]["name"] == "Sam"  # navigable to subject
        assert any("Sam works nights" in t["body"] for t in data["thoughts"])  # the link
    finally:
        await client.close()


async def test_thought_and_gap_detail_404_for_unknown(populated):
    store, _ = populated
    client = await _client(store)
    try:
        assert (await client.get("/api/brain/thought/nope")).status == 404
        assert (await client.get("/api/brain/gap/nope")).status == 404
    finally:
        await client.close()


async def test_outreach_endpoint_merges_sends_and_deliberations(populated):
    # The reaching-out history shows both messages sent AND the choices to wait,
    # newest first — so staying quiet reads as a decision, not an absence.
    store, _ = populated
    await store.record_outreach_decision(
        acted=False, candidate="curiosity:g1",
        reasoning="they only just went quiet", reconsider_minutes=3.0,
    )
    await store.log_outreach("telegram", "curiosity", counts_to_budget=True)
    client = await _client(store)
    try:
        resp = await client.get("/api/brain/outreach")
        assert resp.status == 200
        data = await resp.json()
        kinds = [e["kind"] for e in data]
        assert "sent" in kinds and "consult" in kinds  # both surfaced
        assert data[0]["kind"] == "sent"  # the send is newest
        held = next(e for e in data if e["kind"] == "consult")
        assert held["acted"] is False and "went quiet" in held["reasoning"]
    finally:
        await client.close()


async def test_dreams_endpoint_parses_stats_and_duration(populated):
    store, _ = populated
    dream_id = await store.start_dream(at="2026-06-25T14:00:00+00:00")
    await store.finish_dream(
        dream_id,
        summary="2 thoughts faded, 1 merged.",
        stats={"thoughts_faded": 2, "merged": 1},
        wonders="I wonder what Sam does for a living.",
    )
    client = await _client(store)
    try:
        resp = await client.get("/api/brain/dreams")
        assert resp.status == 200
        data = await resp.json()
        assert len(data) == 1
        d = data[0]
        assert d["stats"]["thoughts_faded"] == 2  # JSON parsed, not a raw string
        assert "Sam" in d["wonders"]
        assert d["duration_seconds"] is not None and d["duration_seconds"] > 0
    finally:
        await client.close()


