"""Token tracking — the LLM usage ledger + the metering seam.

Every brain-model call is metered in one place (UsageMeter, installed in Brain.__init__)
and labelled by what it was for (a contextvar set right before the call). The load-bearing
bits: the meter forwards the response untouched, records one row with the right kind +
token counts, and never lets a bookkeeping failure break a turn; the store rolls the rows
up by activity; and generated media is counted as units (it isn't token-priced).
"""

import pytest

from moko.agent import Brain
from moko.agent.usage import UsageMeter, usage_kind
from moko.config.config import Config
from moko.config.paths import ensure_home
from moko.llm.base import LLMResponse
from moko.store import Store

CFG = Config(companion_name="Jordan", timezone="Asia/Tokyo",
             sleep_start="23:00", sleep_end="07:00", proactivity="moderate")


class _Fake:
    model = "fake-model"
    supports_vision = True
    needs_scaffolding = True

    async def complete(self, system, messages, **kw):
        return LLMResponse("ok", input_tokens=10, output_tokens=5,
                           cache_read_tokens=3, cache_write_tokens=2)

    async def converse(self, system, messages, **kw):
        return LLMResponse("ok", input_tokens=100, output_tokens=20)


class _ReplyLLM:
    """A minimal brain for a real respond() turn — converse reports token usage."""

    model = "reply-fake"
    supports_vision = False

    async def converse(self, system, messages, *, tools, execute, max_tokens=1024,
                       temperature=1.0, max_steps=6, **kw):
        return LLMResponse("hi", input_tokens=1234, output_tokens=56)

    async def complete(self, system, messages, **kw):
        return LLMResponse("hi", input_tokens=7, output_tokens=3)


@pytest.fixture
async def store(tmp_path):
    s = await Store.open(tmp_path / "moko.db")
    try:
        yield s
    finally:
        await s.close()


# ── the meter ───────────────────────────────────────────────────────────────────
async def test_meter_records_one_row_with_the_context_label(store):
    meter = UsageMeter(_Fake(), store)
    usage_kind.set("reflection")
    resp = await meter.complete("sys", [])
    assert resp.text == "ok"  # forwarded unchanged

    rows = await store.usage.recent_llm_calls()
    assert len(rows) == 1
    r = rows[0]
    assert r["kind"] == "reflection" and r["model"] == "fake-model"
    assert (r["input_tokens"], r["output_tokens"]) == (10, 5)
    assert (r["cache_read_tokens"], r["cache_write_tokens"]) == (3, 2)


async def test_meter_is_transparent_about_capabilities(store):
    meter = UsageMeter(_Fake(), store)
    assert meter.model == "fake-model" and meter.supports_vision is True
    # The flag whose absence silently disabled scaffolding — pin it at the seam.
    assert meter.needs_scaffolding is True


async def test_meter_captures_full_io_and_announces_on_the_feed(store):
    import asyncio

    from moko import events
    from moko.llm.base import Msg, System

    sub = events.subscribe(backfill=False)
    try:
        meter = UsageMeter(_Fake(), store)
        usage_kind.set("reply")
        await meter.complete(
            System(stable="you are Moko", volatile="it is Tuesday"),
            [Msg("user", "what kind of cat do you have?")],
        )
        ev = await asyncio.wait_for(sub.queue.get(), timeout=1)
    finally:
        sub.close()

    # Every call is announced on the feed, with a deep-link id.
    assert ev["type"] == "llm.call" and ev["kind"] == "reply"
    assert ev["input_tokens"] == 10 and ev["output_tokens"] == 5 and ev["preview"] == "ok"

    # ...and the full I/O is on the row, fetchable by that id.
    detail = await store.usage.llm_call_detail(ev["id"])
    assert "you are Moko" in detail["system_prompt"] and "it is Tuesday" in detail["system_prompt"]
    assert "what kind of cat" in detail["request"]  # the exact request, flattened
    assert detail["response"] == "ok"
    # The summary list stays light — no big I/O blobs in it.
    assert "system_prompt" not in (await store.usage.recent_llm_calls())[0]


async def test_meter_serializes_image_content_as_a_placeholder(store):
    import asyncio

    from moko import events
    from moko.llm.base import ImagePart, Msg

    sub = events.subscribe(backfill=False)
    try:
        meter = UsageMeter(_Fake(), store)
        usage_kind.set("vision")
        parts = ["look at this:", ImagePart(b"\xff\xd8", "image/jpeg")]
        await meter.complete("sys", [Msg("user", parts)])
        ev = await asyncio.wait_for(sub.queue.get(), timeout=1)
    finally:
        sub.close()
    req = (await store.usage.llm_call_detail(ev["id"]))["request"]
    assert "look at this:" in req and "[image]" in req  # image noted, not dumped as bytes


async def test_meter_caps_oversized_io_keeping_valid_json(store):
    import json

    from moko.llm.base import Msg, System

    class _Big:
        model = "m"
        supports_vision = False

        async def complete(self, system, messages, **kw):
            return LLMResponse("x" * 200_000, input_tokens=1, output_tokens=1)

        async def converse(self, *a, **k):  # pragma: no cover
            return LLMResponse("ok")

    meter = UsageMeter(_Big(), store)
    usage_kind.set("reply")
    await meter.complete(System(stable="y" * 200_000), [Msg("user", "z" * 200_000)])
    d = await store.usage.llm_call_detail((await store.usage.recent_llm_calls(limit=1))[0]["id"])
    assert len(d["system_prompt"]) == 100_000 and len(d["response"]) == 100_000  # _MAX_IO
    assert json.loads(d["request"])[0]["text"] == "z" * 12_000  # per-message cap, still valid JSON


async def test_meter_stores_a_plain_string_system_prompt(store):
    from moko.llm.base import Msg

    meter = UsageMeter(_Fake(), store)
    usage_kind.set("reply")
    await meter.complete("you are Moko", [Msg("user", "hi")])
    rid = (await store.usage.recent_llm_calls(limit=1))[0]["id"]
    assert (await store.usage.llm_call_detail(rid))["system_prompt"] == "you are Moko"


async def test_meter_captures_tool_calls_and_names_them_on_the_feed(store):
    import asyncio
    import json

    from moko import events
    from moko.llm.base import LLMResponse, Msg, System

    class _ToolLLM:
        model = "fake"

        async def converse(self, system, messages, **kw):
            return LLMResponse(
                "sure!", input_tokens=5, output_tokens=2,
                tool_calls=[{"name": "remember",
                             "input": {"commitments": [{"description": "brush teeth"}]}}],
            )

    sub = events.subscribe(backfill=False)
    try:
        meter = UsageMeter(_ToolLLM(), store)
        usage_kind.set("reply")
        await meter.converse(System(stable="s"), [Msg("user", "remind me")], tools=[], execute=None)
        ev = await asyncio.wait_for(sub.queue.get(), timeout=1)
    finally:
        sub.close()

    assert ev["tools"] == ["remember"]  # the feed lists which tools fired
    calls = json.loads((await store.usage.llm_call_detail(ev["id"]))["tool_calls"])
    assert calls[0]["name"] == "remember"
    # The EXACT structured payload is preserved — the thing that reveals a re-recorded dupe.
    assert calls[0]["input"]["commitments"][0]["description"] == "brush teeth"


async def test_prune_llm_call_io_keeps_tokens_drops_blobs(store):
    rid = await store.usage.add_llm_call(kind="reply", model="m", input_tokens=9, output_tokens=2,
                                    system_prompt="big sys", request="[]", response="hi",
                                    tool_calls='[{"name": "remember"}]')
    await store.conn.execute("UPDATE llm_calls SET created_at = '2000-01-01' WHERE id = ?", (rid,))
    await store.conn.commit()
    assert await store.usage.prune_llm_call_io("2020-01-01T00:00:00+00:00") == 1
    d = await store.usage.llm_call_detail(rid)
    assert d["system_prompt"] is None and d["request"] is None and d["response"] is None
    assert d["tool_calls"] is None  # tool payloads pruned with the other I/O blobs
    assert d["input_tokens"] == 9 and d["kind"] == "reply"  # token ledger preserved


async def test_meter_swallows_a_write_failure(store):
    await store.close()  # writes will now fail
    meter = UsageMeter(_Fake(), store)
    usage_kind.set("reply")
    resp = await meter.converse("sys", [])  # must NOT raise — bookkeeping can't break a turn
    assert resp.text == "ok"


# ── the ledger ────────────────────────────────────────────────────────────────────
async def test_summary_rolls_up_by_activity_biggest_first(store):
    await store.usage.add_llm_call(kind="reply", model="m", input_tokens=100, output_tokens=50)
    await store.usage.add_llm_call(kind="reply", model="m", input_tokens=10, output_tokens=5)
    await store.usage.add_llm_call(kind="dream", model="m", input_tokens=200, output_tokens=20)

    s = await store.usage.llm_call_summary()
    assert s["totals"]["calls"] == 3 and s["totals"]["input_tokens"] == 310
    by = {r["kind"]: r for r in s["by_kind"]}
    assert by["reply"]["calls"] == 2 and by["reply"]["input_tokens"] == 110
    assert s["by_kind"][0]["kind"] == "dream"  # 220 tokens > reply's 165 → listed first


async def test_generated_media_counts_only_outbound(store):
    ensure_home()
    await store.add_media(b"a", kind="image", direction="out", source_kind="inference")
    await store.add_media(b"b", kind="image", direction="out", source_kind="inference")
    await store.add_media(b"v", kind="video", direction="out", source_kind="inference")
    await store.add_media(b"in", kind="image", direction="in", source_kind="message")
    assert await store.generated_media_counts() == {"image": 2, "video": 1}


# ── end to end ─────────────────────────────────────────────────────────────────────
async def test_a_reply_turn_is_metered_as_reply(store):
    ensure_home()
    brain = Brain(store, _ReplyLLM(), CFG)  # Brain wraps the llm in a UsageMeter
    await brain.respond("hello there")
    rows = await store.usage.recent_llm_calls()
    assert any(r["kind"] == "reply" and r["input_tokens"] == 1234 for r in rows)
