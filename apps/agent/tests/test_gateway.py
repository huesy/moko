import asyncio

import pytest

from moko.agent import Brain
from moko.channels import Channel, Inbound
from moko.config.config import Config
from moko.gateway import Gateway, Tick, _approval_verdict
from moko.llm.base import LLMResponse
from moko.store import Store


def test_approval_verdict_requires_a_clean_yes_no():
    # Clean confirmations resolve; anything ambiguous is "unclear" (never runs).
    assert _approval_verdict("yes") == "approve"
    assert _approval_verdict("Yes please!") == "approve"
    assert _approval_verdict("no") == "deny"
    assert _approval_verdict("yes always") == "always"
    # The dangerous cases: a declining or off-topic message must NOT approve.
    assert _approval_verdict("okay but actually don't") == "unclear"
    assert _approval_verdict("ok so what were we talking about?") == "unclear"
    assert _approval_verdict("notify Sarah about it") == "unclear"
    # "always" only as a clean grant — not buried in a sentence.
    assert _approval_verdict("I always say yes to these") == "unclear"

CFG = Config(
    companion_name="Jordan",
    timezone="Asia/Tokyo",
    sleep_start="23:00",
    sleep_end="07:00",
    proactivity="moderate",
)

# A remember payload recording one subject, so we can prove recording ran.
EXTRACTION = {
    "subjects": [{"kind": "place", "name": "Lisbon"}],
    "speaker_sentiment": {"label": "neutral"},
}


class FakeLLM:
    model = "fake"

    async def converse(self, system, messages, *, tools, execute, **kw):
        await execute("remember", EXTRACTION)  # record inline, as a real model does
        return LLMResponse("Hello there!")

    async def complete(self, system, messages, **kw):
        return LLMResponse("Just a gentle nudge.")  # how outreach gets phrased


class FakeChannel(Channel):
    """Emits queued inbounds once, then idles; records what was sent."""

    name = "test"

    def __init__(self, inbounds=()):
        self._inbounds = list(inbounds)
        self.sent: list[str] = []

    async def run(self, emit):
        for inbound in self._inbounds:
            await emit(inbound)
        await asyncio.Event().wait()  # idle until cancelled

    async def send(self, text):
        self.sent.append(text)


@pytest.fixture
async def store(tmp_path):
    s = await Store.open(tmp_path / "moko.db")
    try:
        yield s
    finally:
        await s.close()


def _gateway(store, channel):
    brain = Brain(store, FakeLLM(), CFG)
    # Access control lives in channels now; the gateway trusts what it gets.
    return Gateway(store, brain, [channel], heartbeat_seconds=999)


async def test_inbound_replies_and_learns(store):
    ch = FakeChannel()
    gw = _gateway(store, ch)
    await gw.handle(Inbound("I loved Lisbon", "companion-1", "chat-1", "test"))

    assert ch.sent == ["Hello there!"]  # reply sent
    assert await store.find_by_name("Lisbon") is not None  # remember tool ran


async def test_inbound_shows_typing_while_composing(store):
    # The companion should see "typing…" — the keepalive fires at least once
    # immediately, before any reply is sent.
    class TypingChannel(FakeChannel):
        typed = 0

        async def typing(self):
            TypingChannel.typed += 1

    ch = TypingChannel()
    gw = _gateway(store, ch)
    await gw.handle(Inbound("hi", "companion-1", "chat-1", "test"))
    assert TypingChannel.typed >= 1  # typing was signalled during the turn


async def test_idle_research_is_gated_by_the_opt_in(store, monkeypatch):
    # On a quiet idle beat, Moko only pursues its own web research when the companion
    # has opted in (`allow_autonomous_research`). Off (the default) → it never touches
    # the research path and reflects instead; on → it researches and returns before
    # reflecting.
    import moko.gateway as gwmod

    ran = {"research": 0, "reflect": 0}

    async def no_dream(*a, **k):
        return False

    async def no_commitments(now):
        return []

    async def idle(now):
        return None  # nothing to reach out about → the idle branch

    async def spy_research():
        ran["research"] += 1
        return object(), True  # a non-None "gap" → the branch returns before reflect

    async def spy_reflect(now):
        ran["reflect"] += 1

    monkeypatch.setattr(gwmod, "should_dream", no_dream)
    gw = _gateway(store, FakeChannel())
    monkeypatch.setattr(gw.brain, "due_commitment_outreaches", no_commitments)
    monkeypatch.setattr(gw.brain, "consider_outreach", idle)
    monkeypatch.setattr(gw, "_run_research", spy_research)
    monkeypatch.setattr(gw.brain, "reflect_idle", spy_reflect)

    assert gw.brain.config.allow_autonomous_research is False  # secure default
    await gw.handle(Tick())
    assert ran == {"research": 0, "reflect": 1}  # off → skipped research, reflected

    gw.brain.config.allow_autonomous_research = True
    await gw.handle(Tick())
    assert ran == {"research": 1, "reflect": 1}  # on → researched, did not reflect again


async def test_inbound_during_a_dream_makes_should_yield_true(store, monkeypatch):
    # A dream running on the heartbeat gets a `should_yield` it can poll. While the dream
    # is mid-consolidation, a message arriving from the channel side (`_emit`) must flip
    # that callable to True, so the dream can stop early and answer the companion. We use
    # the explicit counter, NOT queue.empty() — the coalesced Tick keeps the queue full.
    import moko.gateway as gwmod

    seen = {}

    async def yes_dream(*a, **k):
        return True

    async def fake_dream(brain, now, *, should_yield=None):
        seen["before"] = should_yield()  # nothing waiting yet → False
        # Simulate a message landing partway through the dream, via the channel's path.
        await gw._emit(Inbound("you up?", "c", "chat-1", "test"))
        seen["after"] = should_yield()  # now an inbound waits → True
        return {}

    monkeypatch.setattr(gwmod, "should_dream", yes_dream)
    monkeypatch.setattr(gwmod, "dream", fake_dream)
    gw = _gateway(store, FakeChannel())
    await gw.handle(Tick())  # enters the dream branch → _run_dream → fake_dream

    assert seen["before"] is False  # a quiet start
    assert seen["after"] is True  # the arriving inbound flipped should_yield


async def test_tick_runs_clean(store):
    gw = _gateway(store, FakeChannel())
    src = await store.add_source("user_stated")
    import datetime as dt

    from moko.clock import now_utc

    await store.add_commitment(
        "remind about thing",
        due_at=(now_utc() - dt.timedelta(hours=1)).isoformat(),
        source_id=src.id,
    )
    await gw.handle(Tick())  # evaluate + maybe deliver, without error (time-dependent)


async def test_tick_delivers_and_records(store, monkeypatch):
    # Pin the decision so this doesn't depend on wall-clock; test the gateway's
    # send + record wiring (the governor itself is unit-tested separately).
    from moko.agent.brain import OutreachProposal

    ch = FakeChannel()
    gw = _gateway(store, ch)
    gap = await store.add_gap(
        "identity of Priya (person)", "dangling_ref", resolution_path="ask", salience=0.7
    )

    async def fake_consider(now):
        # The brain decides + phrases inside consider_outreach now; pin both so
        # this exercises the gateway's send + record wiring, not the consult.
        return "Just a gentle nudge.", OutreachProposal(
            "curiosity", gap.description, counts_to_budget=True, gap_id=gap.id
        )

    async def no_dream(*a, **k):
        return False

    monkeypatch.setattr(gw.brain, "consider_outreach", fake_consider)
    monkeypatch.setattr("moko.gateway.should_dream", no_dream)  # isolate delivery from dreaming
    await gw.handle(Tick())

    assert ch.sent == ["Just a gentle nudge."]  # phrased + sent
    assert await store.count_budget_outreach("1970-01-01T00:00:00+00:00") == 1  # logged to budget
    cur = await store.conn.execute(
        "SELECT last_asked_at FROM knowledge_gaps WHERE id = ?", (gap.id,)
    )
    assert (await cur.fetchone())["last_asked_at"] is not None  # gap paced


async def test_outreach_is_persisted_as_a_message(store, monkeypatch):
    # An outreach must land in Moko's own message history, so the next consult can SEE it
    # already reached out and won't re-send the same thing (the morning-nagging root cause).
    from moko.agent.brain import OutreachProposal

    ch = FakeChannel()
    gw = _gateway(store, ch)

    async def fake_consider(now):
        return "Thinking of you!", OutreachProposal("reconnect", "a warm thought",
                                                    counts_to_budget=True)

    async def no_dream(*a, **k):
        return False

    monkeypatch.setattr(gw.brain, "consider_outreach", fake_consider)
    monkeypatch.setattr("moko.gateway.should_dream", no_dream)
    await gw.handle(Tick())

    msgs = await store.latest_messages(limit=5)
    assert any(m.direction == "out" and m.body == "Thinking of you!" for m in msgs)


class FileChannel(FakeChannel):
    """A channel that can send files, recording them — for the photo-outreach path."""

    can_send_files = True

    def __init__(self, inbounds=()):
        super().__init__(inbounds)
        self.files: list[dict] = []

    async def send_file(self, data, *, filename, caption=None, kind="document"):
        self.files.append({"data": data, "filename": filename, "caption": caption, "kind": kind})


async def test_show_outreach_sends_a_captioned_photo(store, monkeypatch):
    # A "show" outreach delivers as an image with the message as its caption (where the
    # channel supports files) — Moko sharing a picture of something from its world.
    from moko.agent.brain import OutreachProposal
    from moko.config.paths import media_dir

    ch = FileChannel()
    gw = _gateway(store, ch)
    media_dir().mkdir(parents=True, exist_ok=True)
    (media_dir() / "moko-world.png").write_bytes(b"PNGDATA")

    async def fake_consider(now):
        return "look what I found today!", OutreachProposal(
            "show", "the courtyard light", counts_to_budget=True, photo_path="moko-world.png"
        )

    async def no_dream(*a, **k):
        return False

    monkeypatch.setattr(gw.brain, "consider_outreach", fake_consider)
    monkeypatch.setattr("moko.gateway.should_dream", no_dream)
    await gw.handle(Tick())

    assert ch.sent == []  # not a plain-text send
    assert len(ch.files) == 1
    assert ch.files[0]["data"] == b"PNGDATA"
    assert ch.files[0]["caption"] == "look what I found today!"
    assert ch.files[0]["kind"] == "image"
    assert await store.count_budget_outreach("1970-01-01T00:00:00+00:00") == 1  # still recorded


async def test_show_outreach_falls_back_to_text_without_file_support(store, monkeypatch):
    # On a channel that can't send files, a "show" outreach still delivers the message.
    from moko.agent.brain import OutreachProposal

    ch = FakeChannel()  # no can_send_files
    gw = _gateway(store, ch)

    async def fake_consider(now):
        return "look what I found!", OutreachProposal(
            "show", "a thing", counts_to_budget=True, photo_path="whatever.png"
        )

    async def no_dream(*a, **k):
        return False

    monkeypatch.setattr(gw.brain, "consider_outreach", fake_consider)
    monkeypatch.setattr("moko.gateway.should_dream", no_dream)
    await gw.handle(Tick())
    assert ch.sent == ["look what I found!"]  # degraded to text, message preserved


class FailingChannel(FakeChannel):
    async def send(self, text):
        raise RuntimeError("telegram down")


async def test_tick_send_failure_records_nothing(store, monkeypatch):
    # If delivery fails, the gap/commitment must NOT be marked done — otherwise
    # an outreach is silently lost. Nothing logged, nothing paced.
    from moko.agent.brain import OutreachProposal

    gw = _gateway(store, FailingChannel())
    gap = await store.add_gap(
        "identity of Sam (person)", "dangling_ref", resolution_path="ask", salience=0.7
    )

    async def fake_consider(now):
        return "Just a gentle nudge.", OutreachProposal(
            "curiosity", gap.description, counts_to_budget=True, gap_id=gap.id
        )

    async def no_dream(*a, **k):
        return False

    monkeypatch.setattr(gw.brain, "consider_outreach", fake_consider)
    monkeypatch.setattr("moko.gateway.should_dream", no_dream)
    await gw.handle(Tick())  # send raises inside; must be caught and recorded nothing

    assert await store.count_budget_outreach("1970-01-01T00:00:00+00:00") == 0  # not logged
    cur = await store.conn.execute(
        "SELECT last_asked_at FROM knowledge_gaps WHERE id = ?", (gap.id,)
    )
    assert (await cur.fetchone())["last_asked_at"] is None  # gap not falsely paced


async def _drive(gw, msg, answer):
    """Run a turn that blocks inline on approval; deliver `answer` (from the
    channel side, via _emit) once it's waiting. Returns when the turn finishes."""
    task = asyncio.create_task(gw.handle(Inbound(msg, "c", "chat", "test")))
    for _ in range(400):  # wait until the turn is actually awaiting approval
        if gw._approval is not None:
            break
        await asyncio.sleep(0.005)
    if gw._approval is not None:
        await gw._emit(Inbound(answer, "c", "chat", "test"))
    await task


def _danger(name, run, *, category="system"):
    from moko.llm import Tool
    from moko.tools.base import SENSITIVE, AgentTool

    return AgentTool(
        spec=Tool(name=name, description="x", parameters={"type": "object", "properties": {}}),
        category=category, risk=SENSITIVE, run=run,
    )


async def test_sensitive_action_blocks_then_runs_inline_on_yes(store):
    # A sensitive tool → an approval request; a 'yes' resolves it and the SAME turn
    # continues inline (no parking, no separate continuation).
    ran = []

    async def _do(_args):
        ran.append(1)
        return "done the thing"

    class ProposeLLM:
        model = "fake"

        async def converse(self, system, messages, *, tools, execute, **kw):
            r = await execute("do_it", {})  # blocks for approval, then runs
            return LLMResponse(f"All set — {r}")

        async def complete(self, system, messages, **kw):
            return LLMResponse("ok")

    ch = FakeChannel()
    gw = Gateway(store, Brain(store, ProposeLLM(), CFG, tools=[_danger("do_it", _do)]), [ch],
                 heartbeat_seconds=999)
    await _drive(gw, "organise my files", "yes")

    assert ran  # ran inline after the yes
    assert any("Approve" in s for s in ch.sent)  # asked first
    assert any("done the thing" in s for s in ch.sent)  # result flowed into the same turn
    assert await store.pending_approval() is None  # resolved


async def test_multistep_task_completes_in_one_turn(store):
    # "write the file, then read it back": the gated write blocks, approval resolves
    # it inline, and the follow-up read runs in the SAME converse — no continuation.
    from moko.llm import Tool
    from moko.tools.base import SAFE, AgentTool

    steps: list[str] = []

    async def _write(_a):
        steps.append("write")
        return "wrote it"

    async def _read(_a):
        steps.append("read")
        return "file contents"

    empty = {"type": "object", "properties": {}}
    read = AgentTool(
        spec=Tool(name="read_thing", description="x", parameters=empty),
        category="file", risk=SAFE, run=_read,
    )

    class TwoStepLLM:
        model = "fake"

        async def converse(self, system, messages, *, tools, execute, **kw):
            await execute("write_thing", {})  # blocks for approval, then runs
            await execute("read_thing", {})  # free → runs right after, same turn
            return LLMResponse("Here's what it says!")

        async def complete(self, system, messages, **kw):
            return LLMResponse("ok")

    ch = FakeChannel()
    brain = Brain(store, TwoStepLLM(), CFG, tools=[_danger("write_thing", _write), read])
    gw = Gateway(store, brain, [ch], heartbeat_seconds=999)
    await _drive(gw, "write a note then read it back", "yes")

    assert steps == ["write", "read"]  # both ran, one turn
    assert any("what it says" in s for s in ch.sent)


async def test_redirect_drops_the_action_and_suppresses_the_abandoned_reply(store):
    # The companion types a new instruction instead of yes/no while a proposal waits
    # → they're redirecting. The action must NOT run, the abandoned turn's reply is
    # suppressed (their new message gets the real answer), and that message is
    # enqueued as a fresh turn.
    ran = []

    async def _act(_a):
        ran.append(1)
        return "did it"

    class Proposes:
        model = "fake"

        async def converse(self, system, messages, *, tools, execute, **kw):
            r = await execute("do_it", {})
            return LLMResponse(f"got: {r}")

        async def complete(self, system, messages, **kw):
            return LLMResponse("ok")

    ch = FakeChannel()
    gw = Gateway(store, Brain(store, Proposes(), CFG, tools=[_danger("do_it", _act)]), [ch],
                 heartbeat_seconds=999)
    await _drive(gw, "do thing A", "actually, do thing B instead")

    assert not ran  # redirected — never ran
    assert not any("got:" in s for s in ch.sent)  # abandoned turn's reply suppressed
    assert any("Approve" in s for s in ch.sent)  # but the ask did go out
    assert gw.queue.qsize() == 1  # the new message was enqueued for a fresh turn
    assert await store.pending_approval() is None  # nothing left pending


async def test_timeout_sends_a_clear_closure_and_suppresses_the_wrap_up(store, monkeypatch):
    # An ignored approval shouldn't vanish into silence: when the TTL lapses, the
    # companion gets ONE clear line about what was set aside, and the turn's own
    # wrap-up reply is suppressed (so they don't get two messages).
    import moko.gateway as gw_mod

    monkeypatch.setattr(gw_mod, "APPROVAL_TTL_MINUTES", 0.001)  # ~60ms, so it times out
    ran = []

    async def _act(_a):
        ran.append(1)
        return "did it"

    class Proposes:
        model = "fake"

        async def converse(self, system, messages, *, tools, execute, **kw):
            r = await execute("do_it", {})  # blocks, then times out
            return LLMResponse(f"wrap-up: {r}")

        async def complete(self, system, messages, **kw):
            return LLMResponse("ok")

    ch = FakeChannel()
    gw = Gateway(store, Brain(store, Proposes(), CFG, tools=[_danger("do_it", _act)]), [ch],
                 heartbeat_seconds=999)
    await gw.handle(Inbound("do a thing", "c", "chat", "test"))

    assert not ran  # never ran — no approval
    assert any("didn't hear back" in s for s in ch.sent)  # clear closure delivered
    assert not any("wrap-up" in s for s in ch.sent)  # the model's reply was suppressed


async def test_sensitive_action_cancelled_on_no(store):
    ran = []

    async def _act(_a):
        ran.append(1)
        return "did it"

    class ProposeLLM:
        model = "fake"

        async def converse(self, system, messages, *, tools, execute, **kw):
            await execute("do_it", {})
            return LLMResponse("ok")

        async def complete(self, system, messages, **kw):
            return LLMResponse("ok")

    ch = FakeChannel()
    gw = Gateway(store, Brain(store, ProposeLLM(), CFG, tools=[_danger("do_it", _act)]), [ch],
                 heartbeat_seconds=999)
    await _drive(gw, "do a thing", "no")
    assert not ran  # never executed
    pend = await store.pending_approval()
    assert pend is None  # denied, not lingering


async def test_command_runs_on_the_consumer_and_returns_a_result(store):
    # `moko reflect` over the control socket → a Command on the queue → the daemon
    # runs it here (single writer, publishes to the bus) and hands a line back.
    from moko.gateway import Command

    gw = _gateway(store, FakeChannel())
    fut: asyncio.Future = asyncio.get_event_loop().create_future()
    await gw.handle(Command("reflect", fut))
    assert fut.done()
    ok, text = fut.result()  # resolved with an (ok, text) pair now
    assert ok is True  # reflect succeeded
    assert isinstance(text, str) and text  # a non-empty outcome line


async def test_unknown_command_is_reported_not_crashed(store):
    from moko.gateway import Command

    gw = _gateway(store, FakeChannel())
    fut: asyncio.Future = asyncio.get_event_loop().create_future()
    await gw.handle(Command("frobnicate", fut))
    ok, text = fut.result()
    assert ok is True  # an unknown command is a graceful outcome, not a crash
    assert "don't know" in text.lower()  # graceful, no exception


async def test_inbound_marks_outreach_engaged(store):
    gw = _gateway(store, FakeChannel())
    await store.log_outreach("test", "curiosity", counts_to_budget=True)  # engaged = NULL
    assert (await store.recent_outreach(limit=1))[0]["engaged"] is None
    await gw.handle(Inbound("hey Moko", "companion-1", "chat-1", "test"))
    assert (await store.recent_outreach(limit=1))[0]["engaged"]  # the reply marked it engaged


async def test_consumer_isolates_errors(store):
    gw = _gateway(store, FakeChannel())
    consumer = asyncio.create_task(gw._consume())
    await gw.queue.put(object())  # bogus event → handle() raises, must be caught
    await gw.queue.put(Tick())  # a good event after the bad one
    await gw.queue.join()  # both drained → loop survived the bad event
    consumer.cancel()


async def test_run_processes_inbound_then_cancels(store):
    ch = FakeChannel([Inbound("hi from Lisbon", "companion-1", "chat-1", "test")])
    gw = _gateway(store, ch)
    task = asyncio.create_task(gw.run())
    for _ in range(100):  # let the channel emit + the consumer process
        if ch.sent:
            break
        await asyncio.sleep(0.01)
    assert ch.sent == ["Hello there!"]
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_shutdown_drains_queued_inbound(store):
    # A routine restart (SIGTERM) must finish what's already queued, not drop it:
    # producers stop, the consumer drains the backlog, and only then is it
    # cancelled — so the companion's queued/in-flight message isn't lost.
    ch = FakeChannel()
    gw = _gateway(store, ch)
    for i in range(3):
        gw.queue.put_nowait(Inbound(f"msg {i}", "companion-1", "chat-1", "test"))
    task = asyncio.create_task(gw.run())
    await asyncio.sleep(0)  # let run() spin up the consumer + producers
    task.cancel()  # SIGTERM-style shutdown
    with pytest.raises(asyncio.CancelledError):
        await task
    assert ch.sent == ["Hello there!"] * 3  # all three drained + handled, none dropped
