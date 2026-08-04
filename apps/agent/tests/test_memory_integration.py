"""End-to-end memory: drive a scripted multi-turn 'chat' through the real Brain
and Store, then assert what actually landed in the graph and what recall finds.

This is the deterministic version of "run a dummy chat and check the database":
the LLM is scripted at the seam (no network, no flakiness) but everything below
it — the remember tool, subject resolution, supersede, recall — is the real code
path. An opt-in live variant (against a real provider) lives at the bottom,
skipped unless MOKO_LIVE_TEST=1.
"""

import os

import pytest

from moko.agent import Brain
from moko.config.config import Config
from moko.llm.base import LLMResponse
from moko.store import Store

CFG = Config(
    companion_name="Jordan",  # fictional test companion
    timezone="Asia/Tokyo",
    sleep_start="23:00",
    sleep_end="07:00",
    proactivity="moderate",
)


class ScriptedLLM:
    """Plays a fixed script: each turn pops the next (reply, tool_calls) and
    runs the tool calls through the Brain's executor — exactly the shape a real
    model produces. `tool_calls` is a list of (name, args)."""

    model = "scripted"

    def __init__(self, script):
        self._script = list(script)
        self.seen_tool_results = []  # what recall/lookup returned, for assertions

    async def converse(
        self, system, messages, *, tools, execute, max_tokens=1024, temperature=1.0, max_steps=6,
        **kw,
    ):
        reply, tool_calls = self._script.pop(0)
        for name, args in tool_calls:
            result = await execute(name, args)
            self.seen_tool_results.append((name, result))
        return LLMResponse(reply, input_tokens=10, output_tokens=5)


@pytest.fixture
async def store(tmp_path):
    s = await Store.open(tmp_path / "moko.db")
    try:
        yield s
    finally:
        await s.close()


async def test_scripted_conversation_builds_the_graph(store):
    # Three turns: meet Maya, learn she's in Lisbon at Globex, then a follow-up
    # turn where the model recalls her and refines her job before replying.
    meet = {
        "subjects": [{"kind": "person", "name": "Maya"}],
        "relationships": [{"from": "the speaker", "type": "sibling", "to": "Maya"}],
        "speaker_sentiment": {"label": "warm", "valence": 0.6},
    }
    locate = {
        "subjects": [{"kind": "org", "name": "Globex"}, {"kind": "place", "name": "Lisbon"}],
        "relationships": [
            {"from": "Maya", "type": "works_at", "to": "Globex"},
            {"from": "Maya", "type": "lives_in", "to": "Lisbon"},
        ],
        "attributes": [{"subject": "Maya", "key": "occupation", "value": "designer"}],
    }
    refine = {"attributes": [{"subject": "Maya", "key": "occupation", "value": "graphic designer"}]}
    script = [
        ("Lovely — tell me about Maya.", [("remember", meet)]),
        ("Lisbon's wonderful. How's Globex treating her?", [("remember", locate)]),
        ("A graphic designer — got it.", [("lookup", {"name": "Maya"}), ("remember", refine)]),
    ]
    llm = ScriptedLLM(script)
    brain = Brain(store, llm, CFG)

    await brain.respond("My sister Maya just called")
    await brain.respond("She lives in Lisbon and works at Globex")
    turn3 = await brain.respond("She's actually a graphic designer, not just a designer")

    # Subjects exist with the right kinds.
    maya = await store.find_by_name("Maya")
    globex = await store.find_by_name("Globex")
    lisbon = await store.find_by_name("Lisbon")
    assert maya and maya.kind == "person"
    assert globex and globex.kind == "org"
    assert lisbon and lisbon.kind == "place"

    # The refinement superseded (same key, new value) — not a duplicate key.
    occ = [a for a in await store.attributes_for(maya.id) if a.key == "occupation"]
    assert [a.value for a in occ] == ["graphic designer"]

    # Relationships were recorded and link to the companion.
    companion = await store.resolve("person", "Jordan")
    maya_rels = await store.relationships_for(maya.id)
    assert any(r.type == "works_at" for r in maya_rels)
    assert any(r.type == "lives_in" for r in maya_rels)
    assert any(companion.id in (r.from_id, r.to_id) for r in maya_rels)

    # Turn 3's lookup saw Maya before the model replied (grounded reply path).
    assert any(name == "lookup" and "Maya" in res for name, res in llm.seen_tool_results)
    assert turn3.recorded

    # Sentiment travelled with the first message.
    session = await store.current_session()
    msgs = await store.messages_for_session(session.id)
    cur = await store.conn.execute(
        "SELECT sentiment FROM messages WHERE id = ?", (msgs[0].id,)
    )
    assert (await cur.fetchone())["sentiment"] == "warm"

    # And recall — the companion-facing read path — finds her by keyword.
    assert any("Maya" in h.content for h in await store.recall("Maya"))
    assert any("Lisbon" in h.content for h in await store.recall("Lisbon"))


@pytest.mark.skipif(
    os.environ.get("MOKO_LIVE_TEST") != "1",
    reason="live LLM test — set MOKO_LIVE_TEST=1 (and a configured provider) to run",
)
async def test_live_chat_remembers_and_recalls(tmp_path, monkeypatch):
    """Opt-in smoke test against a real provider: a tiny real chat, then assert
    something actually landed in the graph. Off by default (network + cost +
    non-determinism); a manual confidence check, not CI."""
    from moko.config import config as cfg
    from moko.llm import build_client

    monkeypatch.setenv("MOKO_HOME", str(tmp_path))
    config = cfg.load()
    if config is None:
        pytest.skip("no ~/.moko config — run `moko setup` first")
    client = build_client(config)
    if client is None:
        pytest.skip("no brain configured")

    store = await Store.open(tmp_path / "moko.db")
    try:
        brain = Brain(store, client, config)
        await brain.respond("My sister Maya lives in Lisbon and works at Globex.")
        # The model should have recorded Maya; recall must find her.
        hits = await store.recall("Maya")
        assert hits, "live model recorded nothing about Maya"
    finally:
        await store.close()
