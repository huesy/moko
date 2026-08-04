import datetime as dt
import re

import pytest

from moko.agent import Brain
from moko.agent.brain import MOOD_LOW_VALENCE, _stability_from_valence
from moko.clock import Clock, now_utc
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

EXTRACTION = {
    "subjects": [{"kind": "person", "name": "Maya"}],
    "attributes": [
        {
            "subject": "Maya",
            "key": "city",
            "value": "Tokyo",
            "value_type": "string",
            "confidence": 0.9,
        }
    ],
    "relationships": [],
    "speaker_sentiment": {"label": "happy", "valence": 0.5, "confidence": 0.8},
    "thoughts": [
        {"kind": "opinion", "body": "They seem cheerful today.", "shareability": "private"}
    ],
    "gaps": [
        {
            "description": "When is Maya's birthday?",
            "kind": "missing_attr",
            "resolution_path": "ask",
        }
    ],
}


class FakeLLM:
    """Replies, and — if given an `extraction` payload — records it by calling
    the `remember` tool during converse, exactly as a real model would. Pass
    extraction=None to model a turn that learns nothing."""

    model = "fake"

    def __init__(self, reply="Hi there!", extraction=EXTRACTION, tokens=None):
        self.reply = reply
        self.extraction = extraction
        self.tokens = tokens

    async def converse(
        self, system, messages, *, tools, execute, max_tokens=1024, temperature=1.0, max_steps=6,
        **kw,
    ):
        if self.extraction is not None:
            await execute("remember", self.extraction)
        return LLMResponse(
            self.reply,
            input_tokens=self.tokens,
            output_tokens=0 if self.tokens is not None else None,
        )


@pytest.fixture
async def store(tmp_path):
    s = await Store.open(tmp_path / "moko.db")
    try:
        yield s
    finally:
        await s.close()


async def converse(brain, text):
    """One full turn: reply + inline recording, as the gateway drives it."""
    return await brain.respond(text)


class ToolUsingLLM:
    """A fake that calls the lookup tool during converse, proving the brain
    wires a working executor through to the model."""

    model = "fake"

    def __init__(self):
        self.looked_up = None

    async def converse(
        self, system, messages, *, tools, execute, max_tokens=1024, temperature=1.0, max_steps=6,
        **kw,
    ):
        self.looked_up = await execute("lookup", {"name": "Tara"})
        return LLMResponse(f"From memory: {self.looked_up}")


async def test_brain_uses_memory_tools(store):
    # A fact reachable only by tool (Tara isn't the companion's 1-hop core).
    src = await store.add_source("user_stated")
    tara = await store.add_subject("person", "Tara")
    await store.set_attribute(tara.id, "residence", "Brisbane", source_id=src.id)

    llm = ToolUsingLLM()
    turn = await Brain(store, llm, CFG).respond("where does Tara live?")
    assert "Brisbane" in llm.looked_up  # the tool fetched it
    assert "Brisbane" in turn.reply


class _CapturingLLM:
    """Captures the last user turn handed to the model, so a test can assert what
    Moko actually perceived this turn."""

    model = "fake"

    def __init__(self):
        self.last_user = None

    async def converse(
        self, system, messages, *, tools, execute, max_tokens=1024, temperature=1.0, max_steps=6,
        **kw,
    ):
        self.last_user = messages[-1].content if messages else ""
        return LLMResponse("Got your photo — what is it?")


async def test_respond_perceives_and_sources_an_attachment(store):
    from moko.channels.base import Attachment
    from moko.config.paths import media_dir

    llm = _CapturingLLM()  # no supports_vision → the honest no-vision path
    att = Attachment(kind="image", data=b"JPEG", mime="image/jpeg",
                     filename="cat.jpg", caption="my cat", ref="f1")
    await Brain(store, llm, CFG).respond("here you go", attachments=[att])

    # The artifact is stored, sourced, and on disk.
    recent = await store.recent_media()
    assert len(recent) == 1 and recent[0].kind == "image"
    assert (media_dir() / recent[0].path).exists()
    # Moko perceived it in-prompt, framed honestly (can't see inside yet) …
    assert "cat.jpg" in llm.last_user and "can't see or open" in llm.last_user
    # … but the stored message body stays the verbatim text, not the synthetic note.
    session = await store.current_session()
    in_msg = next(m for m in await store.messages_for_session(session.id) if m.direction == "in")
    assert in_msg.body == "here you go"


class _VisionLLM:
    """A vision-capable fake: captures the last user content and records a fact + what
    it saw (`saw`), as a real model would when it can actually see the image."""

    model = "fake"
    supports_vision = True

    def __init__(self):
        self.last = None

    async def converse(self, system, messages, *, tools, execute, max_tokens=1024,
                       temperature=1.0, max_steps=6, **kw):
        self.last = messages[-1].content
        await execute("remember", {
            "subjects": [{"kind": "pet", "name": "Rex"}],
            "attributes": [{"subject": "Rex", "key": "breed", "value": "corgi"}],
            "saw": "a small corgi sitting on a green couch",
        })
        return LLMResponse("What a cute corgi!")


async def test_vision_turn_sends_the_image_and_sources_facts_from_vision(store):
    from moko.channels.base import Attachment
    from moko.llm import ImagePart

    llm = _VisionLLM()
    att = Attachment(kind="image", data=b"JPEGDATA", mime="image/jpeg",
                     filename="rex.jpg", caption="my dog", ref="f")
    await Brain(store, llm, CFG).respond("meet Rex", attachments=[att])

    # The image rode into the turn as content blocks — Moko actually saw it.
    assert isinstance(llm.last, list)
    assert any(isinstance(p, ImagePart) and p.data == b"JPEGDATA" for p in llm.last)
    # What it learned is sourced as `vision` — "from a photo", not the companion's word.
    rex = await store.find_by_name("Rex")
    rows = await store.attributes_with_source(rex.id)
    breed_source = next((kind for attr, kind in rows if attr.key == "breed"), None)
    assert breed_source == "vision"
    # And Moko remembers what it SAW — the gist is on the artifact and recallable, so it
    # survives the conversation rotating away (the whole point).
    art = (await store.recent_media())[0]
    assert art.description == "a small corgi sitting on a green couch"
    hits = await store.recall("corgi couch")
    assert any("couch" in h.content for h in hits)


class _SilentVisionLLM:
    """Vision-capable, but it doesn't note what it saw (no `saw`) — exercising the
    guaranteed backstop. It DOES implement `complete`, which the backstop calls."""

    model = "fake"
    supports_vision = True

    async def converse(self, system, messages, *, tools, execute, max_tokens=1024,
                       temperature=1.0, max_steps=6, **kw):
        return LLMResponse("nice!")  # replies, records nothing, notes nothing

    async def complete(self, system, messages, *, max_tokens=1024, temperature=1.0, **kw):
        return LLMResponse("two cats asleep on a windowsill")


async def test_backstop_describes_image_when_the_reply_didnt(store):
    from moko.channels.base import Attachment

    att = Attachment(kind="image", data=b"CATS", mime="image/jpeg", filename="cats.jpg",
                     caption=None, ref="f")
    await Brain(store, _SilentVisionLLM(), CFG).respond("", attachments=[att])

    # The reply noted nothing, so the backstop's focused call filled the gist in.
    art = (await store.recent_media())[0]
    assert art.description == "two cats asleep on a windowsill"
    assert any("windowsill" in h.content for h in await store.recall("cats windowsill"))


async def test_turn_persists_and_extracts(store):
    brain = Brain(store, FakeLLM(), CFG)
    turn = await converse(brain, "Maya lives in Tokyo now")
    assert turn.reply == "Hi there!"  # FakeLLM's canned reply
    assert turn.recorded  # the remember tool fired this turn

    session = await store.current_session()
    msgs = await store.messages_for_session(session.id)
    assert [m.direction for m in msgs] == ["in", "out"]
    assert all(m.session_id == session.id for m in msgs)

    maya = await store.find_by_name("Maya")
    assert maya is not None
    # "city=Tokyo" is a named place → Tokyo is its own subject, linked (not a string).
    assert await store.find_by_name("Tokyo") is not None
    assert any(r.type == "located_in" for r in await store.relationships_for(maya.id))

    cur = await store.conn.execute("SELECT sentiment FROM messages WHERE id = ?", (msgs[0].id,))
    assert (await cur.fetchone())["sentiment"] == "happy"

    assert any(h.kind == "thought" for h in await store.recall("cheerful"))
    assert any("birthday" in g.description.lower() for g in await store.open_gaps())


THOUGHT_ABOUT_EXTRACTION = {
    "subjects": [{"kind": "pet", "name": "Biscuit"}],
    "attributes": [],
    "relationships": [{"from": "Jordan", "to": "Biscuit", "type": "owns"}],
    "thoughts": [
        {"kind": "feeling", "body": "I've grown fond of Biscuit", "about": "Biscuit"},
        {"kind": "opinion", "body": "Jordan seems happiest outdoors", "about": "me"},
        {"kind": "note", "body": "a thought about nothing in particular"},
    ],
}


async def test_thought_about_links_to_its_subject(store):
    brain = Brain(store, FakeLLM(extraction=THOUGHT_ABOUT_EXTRACTION), CFG)
    await converse(brain, "Biscuit followed me round the garden")

    biscuit = await store.find_by_name("Biscuit")
    linked = await store.thoughts_linked_to(biscuit.id)
    assert [t.body for t in linked] == ["I've grown fond of Biscuit"]

    # "about: me" resolves to the companion, never a new "me" subject.
    assert await store.find_by_name("me") is None
    companion = await store.find_by_name("Jordan")
    comp_linked = await store.thoughts_linked_to(companion.id)
    assert any("happiest outdoors" in t.body for t in comp_linked)


def test_stability_from_valence():
    assert _stability_from_valence(None) == 0.5  # neutral → store default
    assert _stability_from_valence(0.0) == 0.5
    assert _stability_from_valence(0.9) > 0.5  # strongly felt → stickier
    assert _stability_from_valence(-0.9) == _stability_from_valence(0.9)  # magnitude, not sign
    assert 0.0 <= _stability_from_valence(2.0) <= 1.0  # clamped


VALENCE_EXTRACTION = {
    "subjects": [{"kind": "pet", "name": "Biscuit"}],
    "thoughts": [
        {"kind": "feeling", "body": "I adore Biscuit", "about": "Biscuit", "valence": 0.9},
        {"kind": "note", "body": "a flat little housekeeping note"},
    ],
}


async def test_thought_valence_is_recorded_and_raises_stability(store):
    await converse(Brain(store, FakeLLM(extraction=VALENCE_EXTRACTION), CFG), "Biscuit is the best")
    cur = await store.conn.execute("SELECT body, valence, stability FROM thoughts")
    rows = {r["body"]: (r["valence"], r["stability"]) for r in await cur.fetchall()}
    felt = next(v for b, v in rows.items() if "adore" in b)
    flat = next(v for b, v in rows.items() if "flat" in b)
    assert felt[0] == 0.9 and felt[1] > 0.5  # valence stored, stability raised
    assert flat[0] is None and flat[1] == 0.5  # neutral note keeps the default


async def test_commitment_recorded_with_a_future_due_time(store):
    # The model now sees the current date *with year* in its context, so it emits
    # a correct absolute local time. It must persist and land in the FUTURE.
    clock = Clock(CFG.timezone)
    future_local = clock.to_local(now_utc() + dt.timedelta(hours=2)).strftime("%Y-%m-%d %H:%M")
    extraction = {
        "commitments": [{"description": "review the approval workflow", "due_at": future_local}]
    }
    brain = Brain(store, FakeLLM(extraction=extraction), CFG)
    await converse(brain, "remind me in a couple of hours to review the approval workflow")

    open_c = await store.open_commitments()
    assert len(open_c) == 1 and "approval workflow" in open_c[0].description
    assert open_c[0].due_at is not None
    assert dt.datetime.fromisoformat(open_c[0].due_at) > now_utc()  # future — no instant fire
    assert not await store.due_commitments(now_utc().isoformat())  # not yet due


async def test_commitment_hallucinated_past_date_is_guarded(store):
    # The live bug: the model wrote the year as 2025 (the prompt hid the year) → a
    # year-past due time that fired the instant it was created. Even with the year
    # now in context, a due time well in the past is dropped to a someday
    # commitment as a backstop — never stored as immediately-due.
    extraction = {
        "commitments": [{"description": "test ngrok install", "due_at": "2025-06-25 15:53"}]
    }
    brain = Brain(store, FakeLLM(extraction=extraction), CFG)
    await converse(brain, "remind me in an hour to test the ngrok install")

    open_c = await store.open_commitments()
    assert len(open_c) == 1 and open_c[0].due_at is None  # guarded → someday, not fired
    assert not await store.due_commitments(now_utc().isoformat())


async def test_commitment_no_due_time_is_someday(store):
    extraction = {"commitments": [{"description": "water the plants"}]}
    brain = Brain(store, FakeLLM(extraction=extraction), CFG)
    await converse(brain, "remind me to water the plants at some point")
    open_c = await store.open_commitments()
    assert len(open_c) == 1 and open_c[0].due_at is None


async def test_commitment_not_duplicated_on_re_record(store):
    # The live burst bug: on a later, unrelated turn the model re-extracted the
    # reminders it saw in conversation history, spawning a duplicate set. A
    # near-identical commitment recorded again within the window must be skipped.
    clock = Clock(CFG.timezone)
    due = clock.to_local(now_utc() + dt.timedelta(hours=1)).strftime("%Y-%m-%d %H:%M")
    payload = {"commitments": [{"description": "Remind Andrew to stretch", "due_at": due}]}
    brain = Brain(store, FakeLLM(extraction=payload), CFG)
    await converse(brain, "remind me in an hour to stretch")
    await converse(brain, "anyway, what's the weather like?")  # re-records the same one

    stretch = [c for c in await store.open_commitments() if "stretch" in c.description]
    assert len(stretch) == 1  # the re-record was deduped, not a second reminder


async def test_distinct_commitments_are_not_deduped(store):
    # Dedup must not swallow genuinely different reminders that share boilerplate.
    e1 = {"commitments": [{"description": "Remind Andrew to call dad"}]}
    e2 = {"commitments": [{"description": "Remind Andrew to call mum"}]}
    brain = Brain(store, FakeLLM(extraction=e1), CFG)
    await converse(brain, "remind me to call dad")
    brain.llm = FakeLLM(extraction=e2)
    await converse(brain, "and remind me to call mum")
    assert len(await store.open_commitments()) == 2


async def test_same_reminder_different_time_is_not_deduped(store):
    # Dedup keys on description AND due time, so the *same* reminder asked for a
    # different moment still lands as a second commitment (the old time-window
    # dedup would have wrongly swallowed it).
    clock = Clock(CFG.timezone)
    due1 = clock.to_local(now_utc() + dt.timedelta(hours=1)).strftime("%Y-%m-%d %H:%M")
    due2 = clock.to_local(now_utc() + dt.timedelta(days=1)).strftime("%Y-%m-%d %H:%M")
    one = {"commitments": [{"description": "Remind Andrew to stretch", "due_at": due1}]}
    two = {"commitments": [{"description": "Remind Andrew to stretch", "due_at": due2}]}
    brain = Brain(store, FakeLLM(extraction=one), CFG)
    await converse(brain, "remind me in an hour to stretch")
    brain.llm = FakeLLM(extraction=two)
    await converse(brain, "actually also remind me tomorrow to stretch")
    stretch = [c for c in await store.open_commitments() if "stretch" in c.description]
    assert len(stretch) == 2


async def test_recently_closed_commitment_is_not_respawned(store):
    # A reminder surfaced + closed, then re-promised with the same intent + due, is
    # NOT recorded again — the dedup window spans recently-closed ones, not just open.
    # (Half of the morning self-portrait pile-up: re-promising after the first fired.)
    clock = Clock(CFG.timezone)
    due = clock.to_local(now_utc() + dt.timedelta(hours=2)).strftime("%Y-%m-%d %H:%M")
    payload = {"commitments": [{"description": "Send Andrew the report", "due_at": due}]}
    brain = Brain(store, FakeLLM(extraction=payload), CFG)
    await converse(brain, "send me the report in 2 hours")
    [c] = [c for c in await store.open_commitments() if "report" in c.description]
    await store.complete_commitment(c.id)  # the heartbeat surfaced + closed it

    brain.llm = FakeLLM(extraction=payload)  # Moko re-promises the same thing
    await converse(brain, "the report — send it in 2 hours")
    reports = [c for c in await store.list_commitments() if "report" in c.description]
    assert len(reports) == 1  # the closed one; no fresh duplicate


async def test_capability_note_is_honest_about_missing_media(store):
    # With no image model + a non-vision brain, Moko is told (in-prompt) it can't make
    # or see images — so it won't promise a picture it can't produce.
    brain = Brain(store, FakeLLM(), CFG)  # CFG has no image provider; FakeLLM has no vision
    note = brain.studio.capability_note()
    assert "create or generate images" in note and "see images" in note

    # Wire every generative + perceptual sense …
    brain.studio.image = object()  # make images
    brain.studio.video = object()  # make videos
    brain.studio.tts = object()  # speak
    brain.studio.stt = object()  # hear

    class _Vision:
        model = "m"
        supports_vision = True

    brain.studio.llm = _Vision()  # … and see them (the studio owns the capability note)
    assert brain.studio.capability_note() == ""  # nothing to warn about → no note


async def test_reply_prompt_warns_when_moko_cant_make_images(store):
    # The capability note rides in the system prompt of the reply turn.
    class _CapturingSystemLLM:
        model = "fake"

        def __init__(self):
            self.system_text = None

        async def converse(self, system, messages, *, tools, execute, max_tokens=1024,
                           temperature=1.0, max_steps=6, **kw):
            self.system_text = system.text()
            return LLMResponse("ok")

    llm = _CapturingSystemLLM()
    await Brain(store, llm, CFG).respond("could you send me a picture of yourself?")
    assert "create or generate images" in llm.system_text


QUALITY_EXTRACTION = {
    "subjects": [{"kind": "person", "name": "Moko"}, {"kind": "org", "name": "Globex"}],
    "attributes": [
        {"subject": "Jordan", "key": "residence", "value": "another country (unspecified)"}
    ],
    "relationships": [],
    "speaker_sentiment": {"label": "neutral"},
    "thoughts": [],
    "gaps": [
        {
            "description": "Name of the testing server",
            "kind": "missing_attr",
            "resolution_path": "ask",
        },
        {
            "description": "name of the testing server Jordan is working on",
            "kind": "missing_attr",
            "resolution_path": "ask",
        },
    ],
}


async def test_extraction_quality_guards(store):
    brain = Brain(store, FakeLLM(extraction=QUALITY_EXTRACTION), CFG)
    await converse(brain, "working on the server")

    # Moko is never re-created as a person subject.
    cur = await store.conn.execute(
        "SELECT COUNT(*) FROM subjects WHERE lower(name) = 'moko' AND kind = 'person'"
    )
    assert (await cur.fetchone())[0] == 0

    # A vague non-value ("...unspecified") is not stored as a fact.
    companion = await store.resolve("person", "Jordan")
    assert all(a.key != "residence" for a in await store.attributes_for(companion.id))

    # The two rephrasings of the server question collapse to one gap.
    server_gaps = [g for g in await store.open_gaps(limit=50) if "server" in g.description.lower()]
    assert len(server_gaps) == 1


LINK_EXTRACTION = {
    "subjects": [{"kind": "pet", "name": "Biscuit"}],
    "relationships": [{"from": "the speaker", "type": "owns", "to": "Biscuit"}],
    "speaker_sentiment": {"label": "casual"},
}


async def test_self_reference_links_to_companion(store):
    brain = Brain(store, FakeLLM(extraction=LINK_EXTRACTION), CFG)
    await converse(brain, "my dog's name is Biscuit")

    # No junk "speaker" subject is created for the user.
    cur = await store.conn.execute(
        "SELECT COUNT(*) FROM subjects WHERE lower(name) IN ('speaker', 'the speaker')"
    )
    assert (await cur.fetchone())[0] == 0

    # Biscuit is linked to the companion, so it surfaces in their slice —
    # which is what lets it survive a session rotation.
    companion = await store.resolve("person", "Jordan")
    sl = await store.slice([companion.id])
    assert any(e.name == "Biscuit" for e in sl.subjects)


ORPHAN_PET = {
    "subjects": [{"kind": "pet", "name": "Rex"}],
    "relationships": [],
    "speaker_sentiment": {"label": "neutral"},
}


async def test_unlinked_possession_opens_ownership_gap(store):
    # The model recorded a pet but didn't link it to anyone — Moko shouldn't
    # assume it's the companion's; it should plan to ask whose it is.
    brain = Brain(store, FakeLLM(extraction=ORPHAN_PET), CFG)
    await converse(brain, "there's a dog called Rex")

    assert await store.find_by_name("Rex") is not None  # still recorded
    gaps = [g.description.lower() for g in await store.open_gaps(limit=50)]
    assert any("rex" in g and "own" in g for g in gaps)


async def test_linked_possession_no_ownership_gap(store):
    # When the link IS present, no "who owns it" gap is opened.
    brain = Brain(store, FakeLLM(extraction=LINK_EXTRACTION), CFG)
    await converse(brain, "my dog's name is Biscuit")
    gaps = [g.description.lower() for g in await store.open_gaps(limit=50)]
    assert not any("who owns biscuit" in g for g in gaps)


ROLE_WORD_EXTRACTION = {
    "subjects": [{"kind": "person", "name": "brother"}],
    "relationships": [{"from": "the speaker", "type": "sibling", "to": "brother"}],
    "speaker_sentiment": {"label": "neutral"},
}


async def test_role_word_becomes_name_gap_not_subject(store):
    # "my brother" must not create a subject literally named "brother"; instead
    # Moko opens a gap to learn the name.
    brain = Brain(store, FakeLLM(extraction=ROLE_WORD_EXTRACTION), CFG)
    await converse(brain, "I was talking to my brother")

    assert await store.find_by_name("brother") is None  # no junk subject
    cur = await store.conn.execute(
        "SELECT COUNT(*) FROM subjects WHERE lower(name) = 'brother'"
    )
    assert (await cur.fetchone())[0] == 0
    # No bogus relationship to a "brother" node was recorded for the companion.
    companion = await store.resolve("person", "Jordan")
    assert not await store.relationships_for(companion.id)
    # ...and a gap to learn the brother's name was opened.
    gaps = [g.description.lower() for g in await store.open_gaps(limit=50)]
    assert any("brother" in g and "name" in g for g in gaps)


SUBJECT_VALUED_ATTR = {
    "subjects": [{"kind": "org", "name": "Globex"}],
    "attributes": [
        {"subject": "Maya", "key": "works_at", "value": "Globex"},  # → relationship
        {"subject": "Maya", "key": "job_title", "value": "lead graphic designer"},  # scalar
    ],
    "relationships": [{"from": "Maya", "type": "works_at", "to": "Globex"}],
    "speaker_sentiment": {"label": "neutral"},
}


async def test_subject_valued_attribute_becomes_a_relationship(store):
    # A named subject must never be a scalar attribute value — it belongs in a
    # relationship. works_at=Globex is converted (not a string); the real scalar
    # (job_title) and the relationship both survive.
    brain = Brain(store, FakeLLM(extraction=SUBJECT_VALUED_ATTR), CFG)
    await converse(brain, "Maya is the lead graphic designer at Globex")

    maya = await store.find_by_name("Maya")
    attrs = {a.key: a.value for a in await store.attributes_for(maya.id)}
    assert "works_at" not in attrs  # not a string attribute
    assert attrs.get("job_title") == "lead graphic designer"  # scalar kept
    assert any(r.type == "works_at" for r in await store.relationships_for(maya.id))


PLACE_VALUED_ATTRS = {
    "attributes": [
        # A compound key AND compound value — the form research actually produced.
        {"subject": "Melbourne", "key": "country_state", "value": "Victoria, Australia"},
        {"subject": "Egypt", "key": "capital", "value": "Cairo"},
        {"subject": "Melbourne", "key": "mood", "value": "buzzing"},  # not a place → scalar
    ],
}


async def test_place_valued_attributes_become_subjects_and_relationships(store):
    # The reported bug: research stored country_state="Victoria, Australia" as a
    # string. Each named place must become its own subject + relationship.
    await store.add_subject("place", "Melbourne")
    await store.add_subject("place", "Egypt")
    await converse(Brain(store, FakeLLM(extraction=PLACE_VALUED_ATTRS), CFG), "some facts")

    for name in ("Australia", "Victoria", "Cairo"):  # compound value split into subjects
        ent = await store.find_by_name(name)
        assert ent is not None and ent.kind == "place"

    melbourne = await store.find_by_name("Melbourne")
    attrs = {a.key: a.value for a in await store.attributes_for(melbourne.id)}
    assert "country_state" not in attrs  # not a string
    assert attrs.get("mood") == "buzzing"  # non-place scalar kept
    assert "located_in" in {r.type for r in await store.relationships_for(melbourne.id)}

    egypt = await store.find_by_name("Egypt")
    assert any(r.type == "has_capital" for r in await store.relationships_for(egypt.id))


async def test_attribute_change_supersedes_with_history_quietly(store):
    # A value change supersedes (history kept). Judging whether it's a genuine
    # contradiction worth verifying is the dream's LLM job now — NOT a write-time
    # gap. So no verification gap fires during the turn.
    src = await store.add_source("user_stated")
    maya = await store.add_subject("person", "Maya")
    await store.set_attribute(maya.id, "occupation", "teacher", source_id=src.id)

    extraction = {"attributes": [{"subject": "Maya", "key": "occupation", "value": "nurse"}]}
    await converse(Brain(store, FakeLLM(extraction=extraction), CFG), "Maya is a nurse now")

    current = [a.value for a in await store.attributes_for(maya.id) if a.key == "occupation"]
    assert current == ["nurse"]  # superseded — only the new value is current
    history = [
        a.value
        for a in await store.attributes_for(maya.id, current_only=False)
        if a.key == "occupation"
    ]
    assert set(history) == {"teacher", "nurse"}  # nothing deleted
    # No write-time verification gap — that judgment moved to the dream.
    assert not [g for g in await store.open_gaps(limit=50) if g.kind == "verification"]


MOVE_EXTRACTION = {
    "subjects": [{"kind": "place", "name": "Porto"}],
    "relationships": [{"from": "Maya", "type": "lives_in", "to": "Porto"}],
    "speaker_sentiment": {"label": "neutral"},
}


async def test_functional_relationship_supersedes_quietly_when_companion_states_it(store):
    # Maya lives_in Lisbon; "she moved to Porto" supersedes the old edge (kept in
    # history). The companion stated it, so there's NO verify gap — nothing to
    # double-check with them.
    src = await store.add_source("user_stated")
    maya = await store.add_subject("person", "Maya")
    lisbon = await store.add_subject("place", "Lisbon")
    await store.add_relationship(maya.id, lisbon.id, "lives_in", source_id=src.id)

    await converse(Brain(store, FakeLLM(extraction=MOVE_EXTRACTION), CFG), "Maya moved to Porto")

    current = [r for r in await store.relationships_for(maya.id) if r.type == "lives_in"]
    porto = await store.find_by_name("Porto")
    assert len(current) == 1 and current[0].to_id == porto.id  # only Porto is current
    history = await store.relationships_for(maya.id, current_only=False)
    assert any(r.to_id == lisbon.id and r.valid_to for r in history)  # Lisbon kept in history
    assert not [g for g in await store.open_gaps(limit=50) if g.kind == "verification"]


async def test_functional_supersede_verifies_when_not_companion_stated(store):
    # A change Moko inferred/researched (not the companion's word) DOES open a
    # verify gap — that's the case worth double-checking.
    src = await store.add_source("web")
    maya = await store.add_subject("person", "Maya")
    lisbon = await store.add_subject("place", "Lisbon")
    porto = await store.add_subject("place", "Porto")
    await store.add_relationship(maya.id, lisbon.id, "lives_in", source_id=src.id)

    brain = Brain(store, FakeLLM(), CFG)
    await brain._supersede_functional(maya.id, "lives_in", porto.id, [], verify=True)
    assert [g for g in await store.open_gaps(limit=50) if g.kind == "verification"]


async def test_multivalued_relationship_keeps_both(store):
    # friend_of is multi-valued: a second friend doesn't supersede the first.
    await converse(
        Brain(
            store,
            FakeLLM(
                extraction={
                    "subjects": [{"kind": "person", "name": "Wren"}],
                    "relationships": [{"from": "the speaker", "type": "friend_of", "to": "Wren"}],
                }
            ),
            CFG,
        ),
        "my friend Wren",
    )
    await converse(
        Brain(
            store,
            FakeLLM(
                extraction={
                    "subjects": [{"kind": "person", "name": "Sam"}],
                    "relationships": [{"from": "the speaker", "type": "friend_of", "to": "Sam"}],
                }
            ),
            CFG,
        ),
        "my friend Sam",
    )
    companion = await store.resolve("person", "Jordan")
    friends = [r for r in await store.relationships_for(companion.id) if r.type == "friend_of"]
    assert len(friends) == 2  # both kept; no false supersede


class CapturingLLM:
    """Records the system prompt it was handed, replies blandly, records nothing."""

    model = "fake"

    def __init__(self):
        self.system_text = None

    async def converse(
        self, system, messages, *, tools, execute, max_tokens=1024, temperature=1.0, max_steps=6,
        **kw,
    ):
        self.system_text = system.text()
        return LLMResponse("mm.")


THIN_SUBJECT_EXTRACTION = {
    "subjects": [{"kind": "person", "name": "Priya"}],
    "relationships": [{"from": "the speaker", "type": "colleague_of", "to": "Priya"}],
}


async def test_recorded_turn_populates_structural_gaps(store):
    # Mentioning someone with no detail leaves a thin-subject gap in the queue,
    # ready for a later turn to raise — the loop is self-driving, not dependent
    # on an external detect_gaps run.
    brain = Brain(store, FakeLLM(extraction=THIN_SUBJECT_EXTRACTION), CFG)
    await brain.respond("had lunch with my colleague Priya")
    gaps = await store.open_gaps(limit=50)
    assert any(g.kind == "dangling_ref" and "Priya" in g.description for g in gaps)


async def test_respond_surfaces_open_questions(store):
    # Open ask-path gaps are surfaced (numbered) so the model can weave one in or
    # mark it answered — with the instruction on how to close one.
    await store.add_gap(
        "identity of Sam (person)", "dangling_ref", resolution_path="ask", salience=0.7
    )
    llm = CapturingLLM()
    await Brain(store, llm, CFG).respond("hey")
    assert "identity of Sam (person)" in llm.system_text  # offered to the model
    assert "Open questions" in llm.system_text and "resolved_gaps" in llm.system_text


async def test_model_closes_an_answered_gap_inline(store):
    # The semantic resolver: the model marks an open question answered
    # (`resolved_gaps`) and it closes in that same turn — no nightly pass.
    gap = await store.add_gap(
        "Is HeroQuest the reprint or original?", "open_curiosity",
        resolution_path="ask", salience=0.7,
    )

    class AnswersLLM:
        model = "fake"

        async def converse(self, system, messages, *, tools, execute, **kw):
            await execute(  # the companion just answered question #1
                "remember", {"resolved_gaps": [1], "speaker_sentiment": {"label": "happy"}}
            )
            return LLMResponse("Got it — the reprint!")

        async def complete(self, system, messages, **kw):
            return LLMResponse("ok")

    await Brain(store, AnswersLLM(), CFG).respond("It's the reprint, brand new")
    assert (await store.get_gap(gap.id)).status == "resolved"  # closed inline


def _fake_tool(name, risk):
    from moko.llm.base import Tool
    from moko.tools.base import AgentTool

    async def _run(args):
        return f"ran {name}"

    return AgentTool(
        spec=Tool(name=name, description="x", parameters={"type": "object", "properties": {}}),
        category="demo",
        risk=risk,
        run=_run,
    )


class CallToolLLM:
    """Calls one named tool via the executor, then replies."""

    model = "fake"

    def __init__(self, tool_name):
        self.tool_name = tool_name
        self.result = None

    async def converse(self, system, messages, *, tools, execute, **kw):
        self.result = await execute(self.tool_name, {})
        return LLMResponse("done")


async def test_safe_tool_runs_through_the_gate(store):
    from moko.tools.base import SAFE

    llm = CallToolLLM("do_safe")
    await Brain(store, llm, CFG, tools=[_fake_tool("do_safe", SAFE)]).respond("go")
    assert llm.result == "ran do_safe"  # safe → executed


async def test_sensitive_tool_blocks_for_approval_and_denies(store):
    from moko.tools.base import SENSITIVE

    async def _deny(*_a):
        return "deny"  # the companion said no

    llm = CallToolLLM("do_danger")
    await Brain(store, llm, CFG, tools=[_fake_tool("do_danger", SENSITIVE)]).respond(
        "go", request_approval=_deny
    )
    assert "ran" not in llm.result  # NOT executed on a deny


async def test_approved_action_executes_inline(store):
    from moko.tools.base import SENSITIVE

    async def _approve(*_a):
        return "approve"  # the companion said yes

    llm = CallToolLLM("do_danger")
    await Brain(store, llm, CFG, tools=[_fake_tool("do_danger", SENSITIVE)]).respond(
        "go", request_approval=_approve
    )
    assert llm.result == "ran do_danger"  # approved → ran inline, same turn


class CallTwiceLLM:
    """Calls one tool twice in a turn, then replies — to prove how a re-proposal of
    the same action is handled (no re-ask after a deny; free after an 'always')."""

    model = "fake"

    def __init__(self, tool_name):
        self.tool_name = tool_name
        self.results: list[str] = []

    async def converse(self, system, messages, *, tools, execute, **kw):
        self.results.append(await execute(self.tool_name, {}))
        self.results.append(await execute(self.tool_name, {}))
        return LLMResponse("done")

    async def complete(self, system, messages, **kw):
        return LLMResponse("ok")


async def test_denied_action_is_not_reasked_when_reproposed(store):
    # "No means no": a denied action that the model re-proposes verbatim this turn
    # is NOT put to the companion again, and never runs.
    from moko.tools.base import SENSITIVE

    asked: list[str] = []

    async def _approver(payload, desc, cat, always_ok):
        asked.append(desc)
        return "deny"

    llm = CallTwiceLLM("do_danger")
    await Brain(store, llm, CFG, tools=[_fake_tool("do_danger", SENSITIVE)]).respond(
        "go", request_approval=_approver
    )
    assert len(asked) == 1  # asked once; the re-proposal short-circuited
    assert all("ran" not in (r or "") for r in llm.results)  # never executed


async def test_always_trusts_only_that_boundary_class(store):
    # "always" frees the action's *key* for the rest of the session — a second call
    # of the same class runs free, but the affordance is only offered for a freeable
    # GATE-tier key.
    from moko.llm import Tool
    from moko.tools.base import SENSITIVE, AgentTool, GateDecision
    from moko.tools.consent import ALLOW, ASK
    from moko.tools.shell import GATE

    def _classify(args, policy=None):  # mimics a download: GATE, key "network"
        if (policy or {}).get("network") == "allow":
            return GateDecision(ALLOW, "", "free", "network")
        return GateDecision(ASK, "downloads from the network", GATE, "network")

    ran: list[int] = []

    async def _run(_a):
        ran.append(1)
        return "fetched"

    tool = AgentTool(
        spec=Tool(name="fetch", description="x", parameters={"type": "object", "properties": {}}),
        category="system", risk=SENSITIVE, run=_run, classify=_classify,
    )
    offered: list[bool] = []

    async def _approver(payload, desc, cat, always_ok):
        offered.append(always_ok)
        return "always"

    llm = CallTwiceLLM("fetch")
    await Brain(store, llm, CFG, tools=[tool]).respond("go", request_approval=_approver)
    assert offered == [True]  # asked once, and "always" was offered (freeable key)
    assert len(ran) == 2  # first ran on the grant, second ran free via the trusted key


async def test_typed_always_grants_no_standing_trust_for_a_nonfreeable_key(store):
    # The companion can *type* "yes always" for anything, but a non-freeable key
    # (e.g. author_skill → self-modification) must stay always-ask: it runs once,
    # but no standing policy is written.
    from moko.config.policy import load_policy
    from moko.llm import Tool
    from moko.tools.base import SENSITIVE, AgentTool, GateDecision
    from moko.tools.consent import ALLOW, ASK
    from moko.tools.shell import GATE

    def _classify(args, policy=None):  # GATE, but a NON-freeable key
        if (policy or {}).get("skill") == "allow":
            return GateDecision(ALLOW, "", "free", "skill")
        return GateDecision(ASK, "writes a new skill", GATE, "skill")

    ran: list[int] = []

    async def _run(_a):
        ran.append(1)
        return "authored"

    tool = AgentTool(
        spec=Tool(name="author_skill", description="x",
                  parameters={"type": "object", "properties": {}}),
        category="skill", risk=SENSITIVE, run=_run, classify=_classify,
    )
    offered: list[bool] = []

    async def _approver(payload, desc, cat, always_ok):
        offered.append(always_ok)
        return "always"

    llm = CallTwiceLLM("author_skill")
    await Brain(store, llm, CFG, tools=[tool]).respond("go", request_approval=_approver)
    assert offered == [False, False]  # "always" never offered, and re-asked each time
    assert load_policy().get("skill") != "allow"  # typed "always" granted no standing trust
    assert len(ran) == 2  # each call individually approved (one-time), none ran free


async def test_out_of_range_valence_and_confidence_are_clamped(store):
    extraction = {
        "subjects": [{"kind": "pet", "name": "Biscuit"}],
        "attributes": [{"subject": "Biscuit", "key": "age", "value": "3",
                        "value_type": "number", "confidence": 5.0}],
        "thoughts": [{"kind": "feeling", "body": "adore it", "about": "Biscuit", "valence": 2.0}],
    }
    await converse(Brain(store, FakeLLM(extraction=extraction), CFG), "Biscuit is great")
    cur = await store.conn.execute("SELECT valence FROM thoughts WHERE body = 'adore it'")
    assert (await cur.fetchone())["valence"] == 1.0  # clamped to −1..1
    biscuit = await store.find_by_name("Biscuit")
    age = next(a for a in await store.attributes_for(biscuit.id) if a.key == "age")
    assert age.confidence == 1.0  # clamped to 0..1


async def test_blocked_classify_refuses_without_offering_a_workaround(store):
    # A hard block isn't a hurdle to route around — the refusal says so, and no
    # approval is ever requested.
    from moko.llm import Tool
    from moko.tools.base import SENSITIVE, AgentTool, GateDecision
    from moko.tools.consent import DENY

    async def _approver(*_a):  # must never be called for a blocked action
        raise AssertionError("blocked action must not ask for approval")

    tool = AgentTool(
        spec=Tool(name="nuke", description="x", parameters={"type": "object", "properties": {}}),
        category="system", risk=SENSITIVE, run=lambda _a: _never(),
        classify=lambda a, p=None: GateDecision(DENY, "that's off-limits", "blocked"),
    )
    llm = CallToolLLM("nuke")
    await Brain(store, llm, CFG, tools=[tool]).respond("go", request_approval=_approver)
    assert "won't try to work around it" in llm.result


async def _never():
    raise AssertionError("a blocked tool must never run")


async def test_research_gap_records_from_web_and_resolves(store):
    from moko.tools.base import SAFE

    lisbon = await store.add_subject("place", "Lisbon")
    gap = await store.add_gap(
        "identity of Lisbon (place)", "dangling_ref", about_id=lisbon.id,
        resolution_path="research", salience=0.4,
    )

    class ResearchLLM:
        model = "fake"

        async def converse(self, system, messages, *, tools, execute, **kw):
            await execute("web_search", {"query": "Lisbon"})  # looks it up itself
            fact = {"subject": "Lisbon", "key": "summary", "value": "capital of Portugal"}
            await execute("remember", {"attributes": [fact]})
            return LLMResponse("")

    brain = Brain(store, ResearchLLM(), CFG, tools=[_fake_tool("web_search", SAFE)])
    assert await brain.research_gap(gap)  # recorded
    assert any(a.key == "summary" for a in await store.attributes_for(lisbon.id))  # web fact stored
    assert (await store.get_gap(gap.id)).status == "resolved"  # answered itself, gap closed
    # the fact is web-sourced and records where it came from (provenance)
    cur = await store.conn.execute(
        "SELECT src.kind, src.ref FROM attributes a"
        " JOIN sources src ON src.id = a.source_id JOIN subjects e ON e.id = a.subject_id"
        " WHERE e.name='Lisbon' AND a.key='summary'"
    )
    row = await cur.fetchone()
    assert row["kind"] == "web" and "Lisbon" in row["ref"]


async def test_reply_loop_marks_web_read_facts_as_web_not_companion(store):
    # A fact the model records AFTER reading the web in a reply turn must be
    # web-sourced (untrusted), not stamped as the companion's own word — otherwise
    # it silently supersedes known facts and skips the verification gap.
    from moko.llm import Tool
    from moko.tools.base import SAFE, AgentTool

    async def _fetch(args):
        return "Germany's population is about 84 million"

    web = AgentTool(
        spec=Tool(name="web_fetch", description="x",
                  parameters={"type": "object", "properties": {"url": {"type": "string"}}}),
        category="web", risk=SAFE, run=_fetch,
    )

    class WebThenRememberLLM:
        model = "fake"

        async def converse(self, system, messages, *, tools, execute, **kw):
            await execute("web_fetch", {"url": "http://example/germany"})
            await execute("remember", {
                "subjects": [{"kind": "place", "name": "Germany"}],
                "attributes": [{"subject": "Germany", "key": "population", "value": "84 million"}],
            })
            return LLMResponse("About 84 million.")

    await Brain(store, WebThenRememberLLM(), CFG, tools=[web]).respond("germany's population?")
    cur = await store.conn.execute(
        "SELECT s.kind, s.ref FROM attributes a JOIN sources s ON s.id = a.source_id"
        " JOIN subjects e ON e.id = a.subject_id WHERE e.name='Germany' AND a.key='population'"
    )
    row = await cur.fetchone()
    assert row["kind"] == "web" and "germany" in row["ref"]  # provenance is the page, not the chat


async def test_research_loop_offers_only_web_and_memory_tools(store):
    # The untrusted-web research loop runs with no human watching, so it must NOT
    # be handed any side-effecting tool — an injected page can't reach one.
    place = await store.add_subject("place", "Lisbon")
    gap = await store.add_gap(
        "identity of Lisbon (place)", "dangling_ref", about_id=place.id,
        resolution_path="research", salience=0.4,
    )
    captured: dict = {}

    class CaptureLLM:
        model = "fake"

        async def converse(self, system, messages, *, tools, execute, **kw):
            captured["names"] = {t.name for t in tools}
            return LLMResponse("")

    brain = Brain(store, CaptureLLM(), CFG)  # default ALL_TOOLS
    await brain.research_gap(gap)
    names = captured["names"]
    assert {"web_search", "web_fetch", "remember"} <= names  # web + memory present
    for gated in ("execute_command", "write_file", "secrets", "author_skill"):
        assert gated not in names, gated  # no side-effecting tool in the loop


async def test_autonomous_paths_never_carry_a_stale_approver(store):
    # The bug: respond set self._request_approval and never cleared it, so a later
    # heartbeat path could fire an out-of-context approval (and block the consumer).
    # Every autonomous entrypoint clears it first, even on an early fuse return.
    async def _approver(*_a):
        return "approve"

    brain = Brain(store, FakeLLM(), CFG)
    brain._request_approval = _approver
    await brain.consider_outreach(_NIGHT)  # early-returns in quiet hours, still clears
    assert brain._request_approval is None

    brain._request_approval = _approver
    await brain.reflect_idle(_NIGHT)
    assert brain._request_approval is None

    place = await store.add_subject("place", "Lisbon")
    gap = await store.add_gap(
        "identity of Lisbon (place)", "dangling_ref", about_id=place.id,
        resolution_path="research", salience=0.4,
    )
    brain._request_approval = _approver
    await brain.research_gap(gap)
    assert brain._request_approval is None


async def test_respond_emits_tool_events(store):
    # on_event fires for each tool call so a UI (chat verbose) can trace it.
    events = []
    brain = Brain(store, FakeLLM(), CFG)
    turn = await brain.respond("Maya lives in Tokyo now", on_event=events.append)
    assert turn.recorded
    assert any(e["type"] == "tool" and e["tool"] == "remember" for e in events)
    remember = next(e for e in events if e["tool"] == "remember")
    assert remember["args"]["subjects"][0]["name"] == "Maya"  # full payload available


class SkipThenSalvageLLM:
    """Mimics a model that replies without recording (converse leaves recorded
    False), then records on the salvage call — proving the safety net fires."""

    model = "fake"

    def __init__(self):
        self.calls = 0

    async def converse(self, system, messages, *, tools, execute, **kw):
        self.calls += 1
        if self.calls == 1:  # the real turn: reply, but skip remember
            return LLMResponse("Oh nice, tell me more!")
        # the salvage turn: only the remember tool is offered; use it
        await execute("remember", {"subjects": [{"kind": "person", "name": "CaseOh"}]})
        return LLMResponse("")


async def test_salvage_records_when_model_skipped(store):
    llm = SkipThenSalvageLLM()
    turn = await Brain(store, llm, CFG).respond("I love watching CaseOh, a streamer")
    assert llm.calls == 2  # turn + salvage
    assert turn.recorded  # the salvage captured it
    assert await store.find_by_name("CaseOh") is not None


async def test_no_salvage_on_small_talk(store):
    # Pure small talk recording nothing must NOT trigger a salvage call.
    llm = SkipThenSalvageLLM()
    await Brain(store, llm, CFG).respond("ok thanks")
    assert llm.calls == 1  # no second call


async def test_no_record_turn_writes_nothing(store):
    # Small talk: the model replies but never calls remember. Nothing lands
    # in the graph, and the turn reports it didn't record.
    brain = Brain(store, FakeLLM(reply="hello!", extraction=None), CFG)
    turn = await converse(brain, "hey")
    assert turn.reply == "hello!"
    assert not turn.recorded
    cur = await store.conn.execute("SELECT COUNT(*) FROM subjects WHERE kind != 'self'")
    # Only the companion subject exists (created by _ensure_actors), nothing learned.
    assert (await cur.fetchone())[0] == 1


# Local wall-times for the governor (config dream window is 23:00–07:00).
_DAYTIME = dt.datetime(2026, 6, 24, 12, 0, tzinfo=dt.UTC)
_NIGHT = dt.datetime(2026, 6, 24, 2, 0, tzinfo=dt.UTC)


_RECONSIDER_KEY = "outreach_reconsider_at"


async def _ask_gap(store):
    await store.add_gap(
        "identity of Priya (person)", "dangling_ref", resolution_path="ask", salience=0.7
    )


class ConsultLLM:
    """Stands in for the brain's outreach consult: it reads the candidate
    [tokens] offered in the prompt and either acts on one (chosen by kind) or
    holds — so the test drives the *decision* without a real model."""

    model = "fake"

    def __init__(self, *, act_kind=None, reconsider=30.0, message="Hey Andrew 🌸"):
        self.act_kind = act_kind
        self.reconsider = reconsider
        self.message = message
        self.consulted = False

    async def converse(
        self, system, messages, *, tools, execute, max_tokens=1024, temperature=1.0, max_steps=6,
        **kw,
    ):
        if not any(t.name == "decide_outreach" for t in tools):
            return LLMResponse("")
        self.consulted = True
        prompt = "\n".join(m.content for m in messages)
        tokens = re.findall(r"\[([^\]]+)\]", prompt)
        # match "curiosity:<id>" / "wellbeing" / the batched "commitments" token
        chosen = next(
            (t for t in tokens if self.act_kind and t.split(":")[0].startswith(self.act_kind)),
            None,
        )
        if self.act_kind and chosen:
            await execute(
                "decide_outreach",
                {
                    "act": True,
                    "candidate": chosen,
                    "message": self.message,
                    "reconsider_in_minutes": self.reconsider,
                },
            )
        else:
            await execute(
                "decide_outreach", {"act": False, "reconsider_in_minutes": self.reconsider}
            )
        return LLMResponse("")

    async def complete(self, system, messages, **kw):
        return LLMResponse("composed")


async def test_consider_outreach_silent_in_quiet_hours(store):
    await _ask_gap(store)
    llm = ConsultLLM(act_kind="curiosity")
    assert await Brain(store, llm, CFG).consider_outreach(_NIGHT) is None
    assert not llm.consulted  # quiet hours short-circuits before any LLM call


async def test_consider_outreach_acts_on_curiosity_when_clear(store):
    await _ask_gap(store)
    llm = ConsultLLM(act_kind="curiosity")
    result = await Brain(store, llm, CFG).consider_outreach(_DAYTIME)
    assert result is not None
    text, proposal = result
    assert text and proposal.kind == "curiosity" and proposal.counts_to_budget


async def test_consider_outreach_holds_when_brain_declines(store):
    await _ask_gap(store)
    llm = ConsultLLM(act_kind=None, reconsider=5)
    brain = Brain(store, llm, CFG)
    assert await brain.consider_outreach(_DAYTIME) is None
    assert llm.consulted
    # A hold persists a reconsider time so it doesn't re-consult every tick.
    assert await store.get_state(_RECONSIDER_KEY) is not None


async def test_consider_outreach_respects_the_reconsider_gate(store):
    await _ask_gap(store)
    future = (now_utc() + dt.timedelta(hours=3)).isoformat()
    await store.set_state(_RECONSIDER_KEY, future)
    llm = ConsultLLM(act_kind="curiosity")
    assert await Brain(store, llm, CFG).consider_outreach(_DAYTIME) is None
    assert not llm.consulted  # honoured the wait — no LLM call


async def test_force_outreach_bypasses_the_fuses(store):
    # `moko outreach` runs with force=True: it skips the deterministic floor —
    # quiet hours AND a pending reconsider — so the brain's own judgment is
    # exercised on demand (it still gets to decide to act or hold).
    await _ask_gap(store)
    await store.set_state(_RECONSIDER_KEY, (now_utc() + dt.timedelta(hours=3)).isoformat())
    llm = ConsultLLM(act_kind="curiosity")
    result = await Brain(store, llm, CFG).consider_outreach(_NIGHT, force=True)
    assert llm.consulted  # consulted despite quiet hours + a pending reconsider
    assert result is not None  # …and acted, since the (fake) brain chose to


async def test_clear_reconsider_reopens_outreach_after_re_engagement(store):
    # A stale "wait until X" defer is honoured — no consult…
    await _ask_gap(store)
    await store.set_state(_RECONSIDER_KEY, (now_utc() + dt.timedelta(hours=3)).isoformat())
    held = ConsultLLM(act_kind="curiosity")
    assert await Brain(store, held, CFG).consider_outreach(_DAYTIME) is None
    assert not held.consulted
    # …until the companion re-engages, which clears the defer so the next quiet
    # moment consults afresh instead of obeying an outdated decision.
    await Brain(store, held, CFG).clear_reconsider()
    assert await store.get_state(_RECONSIDER_KEY) is None
    fresh = ConsultLLM(act_kind="curiosity")
    assert await Brain(store, fresh, CFG).consider_outreach(_DAYTIME) is not None
    assert fresh.consulted


async def test_due_commitment_fires_deterministically_never_via_the_consult(store):
    # A due reminder fires on its own schedule, NOT through the discretionary consult.
    # Even with a pending reconsider wait, `due_commitment_outreaches` returns it (the wait
    # only governs idle curiosity), and it never spends an LLM *decision* — this is the fix
    # for both the 30s-polling loop and the "your hour's up" conflation.
    src = await store.add_source("user_stated")
    c = await store.add_commitment(
        "call the dentist",
        due_at=(now_utc() - dt.timedelta(hours=1)).isoformat(),
        source_id=src.id,
    )
    await store.set_state(_RECONSIDER_KEY, (now_utc() + dt.timedelta(hours=3)).isoformat())
    llm = ConsultLLM(act_kind="commitment")
    brain = Brain(store, llm, CFG)

    reminders = await brain.due_commitment_outreaches(_DAYTIME)
    assert len(reminders) == 1
    text, proposal = reminders[0]
    assert text and proposal.kind == "commitment" and proposal.commitment_ids == [c.id]
    assert not llm.consulted  # phrased via complete(), never a decide_outreach consult

    # And the discretionary consult no longer even sees the commitment: with the wait
    # pending it stays quiet with no LLM call at all (the every-30s poll is gone).
    assert await brain.consider_outreach(_DAYTIME) is None
    assert not llm.consulted


async def test_due_reminder_deferred_in_quiet_hours(store):
    # A reminder that comes due overnight waits for a humane hour rather than firing at 3am.
    src = await store.add_source("user_stated")
    await store.add_commitment(
        "say good morning",
        due_at=(now_utc() - dt.timedelta(hours=1)).isoformat(),
        source_id=src.id,
    )
    assert await Brain(store, ConsultLLM(), CFG).due_commitment_outreaches(_NIGHT) == []


async def test_consider_outreach_waits_while_conversation_is_warm(store):
    # A message moments ago → curiosity isn't even offered, and with nothing else
    # to say Moko stays quiet without spending an LLM call.
    await _ask_gap(store)
    await store.add_message("telegram", "in", "still chatting", sent_at=now_utc().isoformat())
    llm = ConsultLLM(act_kind="curiosity")
    assert await Brain(store, llm, CFG).consider_outreach(_DAYTIME) is None
    assert not llm.consulted


async def test_consider_outreach_resumes_after_chat_goes_stale(store):
    await _ask_gap(store)
    stale = (now_utc() - dt.timedelta(hours=12)).isoformat()
    await store.add_message("telegram", "in", "talked earlier", sent_at=stale)
    result = await Brain(store, ConsultLLM(act_kind="curiosity"), CFG).consider_outreach(_DAYTIME)
    assert result and result[1].kind == "curiosity"  # quiet long enough → eligible


async def test_curiosity_not_offered_once_ceiling_reached(store):
    await _ask_gap(store)
    for _ in range(3):  # moderate ceiling = 3/day
        await store.log_outreach("cli", "curiosity", counts_to_budget=True)
    llm = ConsultLLM(act_kind="curiosity")
    # Ceiling reached → curiosity isn't a candidate; nothing else → quiet, no consult.
    assert await Brain(store, llm, CFG).consider_outreach(_DAYTIME) is None
    assert not llm.consulted


async def test_due_reminder_ignores_the_curiosity_ceiling(store):
    # A reminder is deterministic — it fires when due regardless of the curiosity budget.
    src = await store.add_source("user_stated")
    await store.add_commitment(
        "call the dentist",
        due_at=(now_utc() - dt.timedelta(hours=1)).isoformat(),
        source_id=src.id,
    )
    for _ in range(3):  # curiosity ceiling spent — irrelevant to reminders
        await store.log_outreach("cli", "curiosity", counts_to_budget=True)
    reminders = await Brain(store, ConsultLLM(), CFG).due_commitment_outreaches(_DAYTIME)
    assert len(reminders) == 1 and reminders[0][1].kind == "commitment"


async def test_each_due_commitment_is_its_own_reminder(store):
    # Each due reminder is a SEPARATE message about just that one thing, completing only
    # itself — so wording/timing can't drift between them (the "your hour's up" conflation,
    # where a stale batch was fired under a fresh request's guise).
    src = await store.add_source("user_stated")
    past = (now_utc() - dt.timedelta(hours=1)).isoformat()
    a = await store.add_commitment("review the approval workflow", due_at=past, source_id=src.id)
    b = await store.add_commitment("test the ngrok install", due_at=past, source_id=src.id)

    brain = Brain(store, ConsultLLM(), CFG)
    reminders = await brain.due_commitment_outreaches(_DAYTIME)
    assert len(reminders) == 2
    assert all(len(p.commitment_ids) == 1 for _, p in reminders)  # one id each, never batched
    assert {p.commitment_ids[0] for _, p in reminders} == {a.id, b.id}

    # Delivering each completes only its own; after both, nothing is left due.
    for _, proposal in reminders:
        await brain.record_outreach(proposal, "cli")
    assert not await store.due_commitments(now_utc().isoformat())


async def _low_mood(store, n=4):
    session = await store.ensure_session()
    for i in range(n):
        m = await store.add_message("cli", "in", f"rough day {i}", session_id=session.id)
        await store.set_message_sentiment(m.id, sentiment="low", valence=-0.6, confidence=0.8)


async def test_consider_outreach_offers_wellbeing_on_low_mood(store):
    await _low_mood(store)
    result = await Brain(store, ConsultLLM(act_kind="wellbeing"), CFG).consider_outreach(_DAYTIME)
    assert result and result[1].kind == "wellbeing" and not result[1].counts_to_budget


async def test_wellbeing_check_in_is_once_a_day(store):
    await _low_mood(store)
    assert await Brain(store, FakeLLM(), CFG)._should_check_in()
    await store.log_outreach("cli", "wellbeing", counts_to_budget=False)  # already checked in
    assert not await Brain(store, FakeLLM(), CFG)._should_check_in()


async def test_good_mood_no_check_in(store):
    session = await store.ensure_session()
    for i in range(4):
        m = await store.add_message("cli", "in", f"great day {i}", session_id=session.id)
        await store.set_message_sentiment(m.id, sentiment="glad", valence=0.6, confidence=0.8)
    assert not await Brain(store, FakeLLM(), CFG)._should_check_in()


async def _seed_affect(store, specs):
    """Seed inbound messages with explicit (valence, arousal, confidence), so the
    well-being gating (arousal floor + confidence weighting) can be exercised."""
    session = await store.ensure_session()
    for i, (valence, arousal, confidence) in enumerate(specs):
        m = await store.add_message("cli", "in", f"msg {i}", session_id=session.id)
        await store.set_message_sentiment(
            m.id, sentiment="seeded", valence=valence, arousal=arousal, confidence=confidence
        )


async def test_keyed_up_negatives_are_not_low_mood(store):
    # Anger / banter: negative valence but high arousal — a different state than the
    # flat/down a check-in is for, so none of these count toward the trend.
    await _seed_affect(store, [(-0.7, 0.85, 0.9)] * 5)
    assert not await Brain(store, FakeLLM(), CFG)._should_check_in()


async def test_unsure_negatives_dont_trigger_a_checkin(store):
    # Calm and negative, but every read is low-confidence — too little signal mass
    # to spend a governed outreach on (confidence is now load-bearing).
    await _seed_affect(store, [(-0.7, 0.2, 0.2)] * 6)
    assert not await Brain(store, FakeLLM(), CFG)._should_check_in()


async def test_genuine_low_mood_still_checks_in(store):
    # Calm-to-flat, negative, and confident — the real signal still surfaces.
    await _seed_affect(store, [(-0.5, 0.25, 0.9)] * 4)
    assert await Brain(store, FakeLLM(), CFG)._should_check_in()


async def test_confidence_weighting_suppresses_a_false_trend(store):
    # Unweighted these would fire (mean valence <= the threshold); but the negatives
    # are all low-confidence, so the confidence-weighted mean stays above it.
    specs = [(0.2, 0.3, 0.9)] * 3 + [(-0.7, 0.3, 0.2)] * 4
    assert sum(v for v, _, _ in specs) / len(specs) <= MOOD_LOW_VALENCE  # old rule would trigger
    await _seed_affect(store, specs)
    assert not await Brain(store, FakeLLM(), CFG)._should_check_in()


class _CaptureSalvageLLM:
    """Captures the tool_choice passed to converse, so a test can assert salvage
    forces `remember` for weak brains and stays elective for capable ones."""

    def __init__(self, needs_scaffolding):
        self.needs_scaffolding = needs_scaffolding
        self.model = "x"
        self.tool_choice = "UNSET"

    async def converse(self, system, messages, *, tools, execute, max_steps=6,
                       tool_choice=None, **kw):
        self.tool_choice = tool_choice
        return LLMResponse("")

    async def complete(self, system, messages, **kw):
        return LLMResponse("")


async def test_salvage_forces_remember_only_for_weak_brains(store):
    weak = _CaptureSalvageLLM(needs_scaffolding=True)
    await Brain(store, weak, CFG)._salvage("sys", "Maya moved to Tokyo", "m1", "cli", "c1", [])
    assert weak.tool_choice == "remember"  # weak brain: force the call

    capable = _CaptureSalvageLLM(needs_scaffolding=False)
    await Brain(store, capable, CFG)._salvage("sys", "Maya moved to Tokyo", "m1", "cli", "c1", [])
    assert capable.tool_choice is None  # capable brain stays elective


class _EmptyRememberLLM:
    """A forced brain that calls `remember` with nothing (as told to, when there's
    genuinely nothing) — to prove an empty forced call writes no provenance row."""

    needs_scaffolding = True
    model = "x"

    async def converse(self, system, messages, *, tools, execute, max_steps=6,
                       tool_choice=None, **kw):
        await execute("remember", {})  # forced, but empty
        return LLMResponse("")

    async def complete(self, system, messages, **kw):
        return LLMResponse("")


async def _source_count(store) -> int:
    cur = await store.conn.execute("SELECT COUNT(*) AS n FROM sources")
    return (await cur.fetchone())["n"]


async def test_salvage_empty_forced_call_records_nothing(store):
    # The data-integrity fix: a forced-empty `remember` must not write an orphan
    # source row, nor report that it recorded anything.
    before = await _source_count(store)
    fired = await Brain(store, _EmptyRememberLLM(), CFG)._salvage(
        "sys", "hmm, not really sure", "m1", "cli", "c1", []
    )
    assert fired is False  # an empty forced call didn't "record"
    assert await _source_count(store) == before  # and left no orphan provenance row


# ── idle reflection ───────────────────────────────────────────────────
class ReflectLLM:
    """Stands in for a reflection. Counts consultations (to prove the fuses
    short-circuit *before* any LLM call); `record=False` models a muse that turns
    up nothing (so the backoff can be tested)."""

    model = "fake"

    def __init__(self, record=True):
        self.calls = 0
        self.record = record

    async def converse(
        self, system, messages, *, tools, execute, max_tokens=1024, temperature=1.0, max_steps=6,
        **kw,
    ):
        self.calls += 1
        if self.record:
            await execute(
                "remember",
                {"thoughts": [{"kind": "feeling", "body": "a quiet musing", "valence": 0.4}]},
            )
        return LLMResponse("")

    async def complete(self, system, messages, **kw):
        return LLMResponse("")


async def _quiet_for(store, hours):
    """A message `hours` ago, so the chat counts as settled (or not)."""
    when = (now_utc() - dt.timedelta(hours=hours)).isoformat()
    await store.add_message("telegram", "in", "earlier chat", sent_at=when)


async def _reflected_ago(store, *, hours, on_last_msg=True):
    """Mark that Moko last reflected `hours` ago (and, by default, that it already
    reflected on the latest message — so the next pass is a *muse*, not fresh)."""
    await store.set_state("reflect_last_at", (now_utc() - dt.timedelta(hours=hours)).isoformat())
    if on_last_msg:
        await store.set_state("reflect_seen_at", await store.last_message_at())


async def test_reflect_idle_forms_a_thought_when_quiet(store):
    await _quiet_for(store, 1)
    llm = ReflectLLM()
    brain = Brain(store, llm, CFG)
    assert await brain.reflect_idle(_DAYTIME) is True
    assert llm.calls == 1
    cur = await store.conn.execute("SELECT COUNT(*) c FROM thoughts")
    assert (await cur.fetchone())["c"] >= 1  # a thought landed
    assert await store.get_state("reflect_last_at") is not None  # interval marker set
    assert await store.get_state("reflect_seen_at") is not None  # fresh-material marker set


async def test_reflect_idle_silent_in_dream_window(store):
    await _quiet_for(store, 1)
    llm = ReflectLLM()
    assert await Brain(store, llm, CFG).reflect_idle(_NIGHT) is False
    assert llm.calls == 0  # the night belongs to dream — no LLM call


async def test_reflect_idle_waits_while_chat_is_warm(store):
    await _quiet_for(store, 0.1)  # ~6 min ago — still mid-conversation (< 10 min)
    llm = ReflectLLM()
    assert await Brain(store, llm, CFG).reflect_idle(_DAYTIME) is False
    assert llm.calls == 0


async def test_reflect_idle_settle_is_shorter_than_outreach(store):
    # Reflection's settle (~10 min) is its own, shorter than outreach's (30 min):
    # 15 min of quiet is enough to muse, though it wouldn't yet be enough to ping.
    await _quiet_for(store, 0.25)  # 15 min ago
    llm = ReflectLLM()
    assert await Brain(store, llm, CFG).reflect_idle(_DAYTIME) is True
    assert llm.calls == 1


async def test_reflect_idle_keeps_musing_without_fresh_material(store):
    # No new message since last time, but the muse interval has passed → Moko
    # still muses on what it already knows.
    await _quiet_for(store, 3)
    await _reflected_ago(store, hours=2)  # 2h > the ~30 min muse interval
    llm = ReflectLLM()
    assert await Brain(store, llm, CFG).reflect_idle(_DAYTIME) is True
    assert llm.calls == 1


async def test_reflect_idle_muse_too_soon_is_skipped(store):
    await _quiet_for(store, 3)
    await _reflected_ago(store, hours=0.1)  # ~6 min < the muse interval
    llm = ReflectLLM()
    assert await Brain(store, llm, CFG).reflect_idle(_DAYTIME) is False
    assert llm.calls == 0


async def test_reflect_idle_empty_muse_backs_off(store):
    await _quiet_for(store, 3)
    await _reflected_ago(store, hours=2)
    llm = ReflectLLM(record=False)  # the muse turns up nothing
    assert await Brain(store, llm, CFG).reflect_idle(_DAYTIME) is False
    assert llm.calls == 1
    assert await store.get_state("reflect_dry") == "1"  # backoff deepened


async def test_reflect_idle_never_reaches_out(store):
    await _quiet_for(store, 1)
    await Brain(store, ReflectLLM(), CFG).reflect_idle(_DAYTIME)
    cur = await store.conn.execute("SELECT COUNT(*) c FROM outreach")
    assert (await cur.fetchone())["c"] == 0  # reflection is private — it never sends


async def test_size_rotation(store):
    brain = Brain(store, FakeLLM(reply="ok"), CFG, session_max_tokens=5)
    first = await store.ensure_session()
    await brain.respond("a fairly long message that easily exceeds the tiny token budget")
    current = await store.current_session()
    assert current.id != first.id
    assert current.started_reason == "size"


async def test_idle_rotation(store):
    brain = Brain(
        store, FakeLLM(reply="ok"), CFG, session_idle_reset_hours=1, session_max_tokens=100_000
    )
    session = await store.ensure_session()
    old = (dt.datetime.now(dt.UTC) - dt.timedelta(hours=3)).isoformat()
    await store.add_message("cli", "in", "earlier", session_id=session.id, sent_at=old)

    await brain.respond("hello again")
    current = await store.current_session()
    assert current.id != session.id
    assert current.started_reason == "idle"


async def test_new_session_command(store):
    brain = Brain(store, FakeLLM(), CFG)
    first = await store.ensure_session()
    second = await brain.new_session("user")
    assert second.id != first.id
    assert (await store.get_session(first.id)).ended_reason == "user"


async def test_rotation_drops_transcript_but_keeps_knowledge(store):
    # The load-bearing invariant: rotating a session drops only the verbatim
    # transcript — the knowledge was consolidated into the graph and survives.
    brain = Brain(store, FakeLLM(), CFG)
    await converse(brain, "Maya lives in Tokyo now")  # learns Maya + Tokyo
    first = await store.current_session()
    second = await brain.new_session("user")  # rotate working memory
    assert second.id != first.id and second.id == (await store.current_session()).id
    assert await store.messages_for_session(second.id) == []  # fresh, no carried transcript
    assert await store.find_by_name("Maya") is not None  # but the knowledge is still there
    assert await store.find_by_name("Tokyo") is not None


class DecidingLLM:
    """Drives the outreach consult: decides via the decide_outreach tool, and
    stashes the exact system + user prompt it was handed — so the test can prove
    the event carries the *real* input the model saw, not a reconstruction."""

    model = "fake"

    def __init__(self, decision: dict):
        self.decision = decision
        self.seen_system: str | None = None
        self.seen_user: str | None = None

    async def converse(
        self, system, messages, *, tools, execute, max_tokens=1024, temperature=1.0, max_steps=6,
        **kw,
    ):
        self.seen_system = system.text()
        self.seen_user = messages[0].content
        await execute(tools[0].name, self.decision)
        return LLMResponse("")


async def test_outreach_consult_event_carries_the_exact_prompt(store, monkeypatch):
    # The "considered reaching out" card hangs the prompt on its event so the
    # monitor can show *why* — live, never persisted. Prove the wiring.
    from moko.agent import brain as brain_mod

    published: list[dict] = []
    monkeypatch.setattr(brain_mod, "_publish_event", published.append)

    decision = {
        "act": True,
        "candidate": "cat",
        "message": "How's Biscuit settling in?",
        "reconsider_in_minutes": 6,
        "reasoning": "A quiet, friendly gap — worth a gentle hello.",
    }
    llm = DecidingLLM(decision)
    brain = Brain(store, llm, CFG)
    candidates = [{"token": "cat", "label": "ask how the new cat is settling in"}]

    await brain._consult_outreach(now_utc(), candidates)

    consult = [e for e in published if e["type"] == "outreach_consult"]
    assert len(consult) == 1
    prompt = consult[0]["prompt"]
    # Both halves captured…
    assert prompt["system"] and prompt["user"]
    # …and they are the exact strings the model saw, not a re-derivation.
    assert prompt["system"] == llm.seen_system
    assert prompt["user"] == llm.seen_user
    # The user turn is the genuine decision input: the candidate, by token + label.
    assert "[cat]" in prompt["user"]
    assert "ask how the new cat is settling in" in prompt["user"]


def test_companion_mood_includes_label_valence_and_arousal():
    from types import SimpleNamespace

    from moko.agent.brain import _companion_mood

    def msg(sentiment, valence, arousal):
        return SimpleNamespace(
            direction="in", sentiment=sentiment, valence=valence, arousal=arousal
        )

    assert _companion_mood([msg("frustrated", -0.4, 0.8)]) == "frustrated (-0.4, keyed-up)"
    assert _companion_mood([msg("tired", -0.2, 0.1)]) == "tired (-0.2, flat)"
    assert _companion_mood([msg("ok", 0.1, 0.5)]) == "ok (+0.1)"  # mid arousal → no word
    assert _companion_mood([]) is None


def test_fact_meta_flags_only_uncertain_or_stale_facts():
    import datetime as dt
    from types import SimpleNamespace

    from moko.agent.memory_tools import _fact_meta
    from moko.clock import now_utc

    recent = now_utc().isoformat()
    old = (now_utc() - dt.timedelta(days=40)).isoformat()
    fresh_sure = SimpleNamespace(confidence=0.95, valid_from=recent, created_at=recent)
    assert _fact_meta(fresh_sure) == ""  # recent + confident → clean
    uncertain = SimpleNamespace(confidence=0.6, valid_from=recent, created_at=recent)
    assert "conf 0.6" in _fact_meta(uncertain)
    stale = SimpleNamespace(confidence=0.95, valid_from=old, created_at=old)
    assert "mo ago" in _fact_meta(stale)


# ── voice + video tools (M10) ──────────────────────────────────────────────────
async def test_generate_video_respects_per_turn_cap(store):
    from moko.media.base import GeneratedVideo

    class _Video:
        model = "m"

        async def generate(self, prompt, **kw):
            return GeneratedVideo(b"VIDEO", "video/mp4")

    brain = Brain(store, FakeLLM(), CFG)
    brain.studio.video = _Video()
    brain.studio._videos_made = 0
    first = await brain.studio._generate_video({"prompt": "a kite in the wind"})
    assert "send_attachment" in first  # made + saved → tells the model to send it
    second = await brain.studio._generate_video({"prompt": "another one"})
    assert "already made a video" in second  # the strict per-turn video cap (1) holds


async def test_generate_video_off_when_unconfigured(store):
    brain = Brain(store, FakeLLM(), CFG)  # CFG configures no video model
    assert brain.studio.video is None
    assert "no video model" in await brain.studio._generate_video({"prompt": "x"})


async def test_speak_records_a_sourced_audio_artifact(store):
    from moko.media.base import GeneratedAudio

    class _TTS:
        model = "m"

        async def synthesize(self, text, *, fmt="mp3"):
            return GeneratedAudio(b"MP3BYTES", "audio/mpeg")

    brain = Brain(store, FakeLLM(), CFG)
    brain.studio.tts = _TTS()
    brain.studio._images_made = 0
    out = await brain.studio._speak({"text": "hello, it's me"})
    assert "send_attachment" in out
    # The voice note is on record as an outbound, sourced artifact Moko made.
    art = (await store.recent_media(limit=1))[0]
    assert art.kind == "audio" and art.direction == "out"


async def test_hearing_transcribes_inbound_voice_note(store):
    from moko.channels.base import Attachment
    from moko.media.base import Transcript

    class _STT:
        model = "m"

        async def transcribe(self, audio, *, mime="audio/ogg"):
            return Transcript("can you remind me to call mum")

    class _CapMsgs:
        model = "fake"

        def __init__(self):
            self.last_user = None

        async def converse(self, system, messages, *, tools, execute, **kw):
            self.last_user = messages[-1].content if messages else None
            return LLMResponse("ok")

    llm = _CapMsgs()
    brain = Brain(store, llm, CFG)
    brain.studio.stt = _STT()
    att = Attachment(kind="audio", data=b"OGGDATA", mime="audio/ogg", filename="voice.ogg")
    await brain.respond("(voice note)", attachments=[att])
    text = (llm.last_user if isinstance(llm.last_user, str)
            else " ".join(p for p in llm.last_user if isinstance(p, str)))
    assert "can you remind me to call mum" in text  # Moko genuinely heard it
    assert "can't see or open" not in text  # not contradicted by the opaque-file note
    # The transcript is the artifact's durable description.
    art = (await store.recent_media(limit=1))[0]
    assert art.kind == "audio" and "call mum" in (art.description or "")


async def test_no_hearing_model_degrades_to_honest_note(store):
    from moko.channels.base import Attachment

    class _CapMsgs:
        model = "fake"

        def __init__(self):
            self.last_user = None

        async def converse(self, system, messages, *, tools, execute, **kw):
            self.last_user = messages[-1].content if messages else None
            return LLMResponse("ok")

    llm = _CapMsgs()
    brain = Brain(store, llm, CFG)  # CFG configures no hearing model
    assert brain.studio.stt is None
    att = Attachment(kind="audio", data=b"OGG", mime="audio/ogg", filename="voice.ogg")
    await brain.respond("(voice note)", attachments=[att])
    text = (llm.last_user if isinstance(llm.last_user, str)
            else " ".join(p for p in llm.last_user if isinstance(p, str)))
    assert "can't see or open" in text  # honest: a file arrived, not yet listenable


class _MessagesCapturingLLM:
    """Captures the full message list handed to the model, to assert the transcript's shape."""

    model = "fake"

    def __init__(self):
        self.messages = None

    async def converse(self, system, messages, *, tools, execute, max_tokens=1024,
                       temperature=1.0, max_steps=6, **kw):
        self.messages = list(messages)
        return LLMResponse("noted")


async def test_transcript_stamps_the_companions_messages_with_local_time(store):
    # Moko is time-aware: each of the companion's messages carries the local time it was sent,
    # so it can answer "how long ago did I tell you…". Moko's own replies stay clean (nothing to
    # parrot). (The reported bug: Moko said it couldn't see the timestamps.)
    import re

    llm = _MessagesCapturingLLM()
    brain = Brain(store, llm, CFG)
    await brain.respond("Maya is a nurse")       # turn 1
    await brain.respond("anyway, how are you?")  # turn 2 — history now includes turn 1

    user_msgs = [m.content for m in llm.messages if m.role == "user"]
    stamped = [u for u in user_msgs if isinstance(u, str) and re.match(r"^\[\w{3} \d\d:\d\d\] ", u)]
    assert any("Maya is a nurse" in u for u in stamped)  # the earlier fact, time-stamped
    # Moko's own reply in the transcript carries no timestamp (so it won't echo one).
    replies = [m.content for m in llm.messages if m.role == "assistant"]
    assert replies and not any(isinstance(a, str) and a.startswith("[") for a in replies)


class _PriorAttrLLM:
    """Records a PAST value via the remember tool's `prior` flag, as the model should."""

    model = "fake"

    async def converse(self, system, messages, *, tools, execute, max_tokens=1024,
                       temperature=1.0, max_steps=6, **kw):
        await execute("remember", {"attributes": [
            {"subject": "Maya", "key": "occupation", "value": "receptionist", "prior": True},
        ]})
        return LLMResponse("noted")


async def test_remember_prior_records_history_not_a_parallel_key(store):
    # The reported bug: telling Moko a PAST value ("she was a receptionist before nurse") made it
    # invent previous_occupation / previous_occupation_2. With `prior: true` it records under the
    # SAME key as history, leaving the current value intact.
    src = await store.add_source("user_stated")
    maya = await store.add_subject("person", "Maya")
    await store.set_attribute(maya.id, "occupation", "doctor", source_id=src.id)

    await Brain(store, _PriorAttrLLM(), CFG).respond("she used to be a receptionist, before that")

    keys = {a.key for a in await store.attributes_for(maya.id, current_only=False)}
    assert keys == {"occupation"}  # no previous_occupation / former_occupation
    current = await store.attributes_for(maya.id)  # current_only
    assert [a.value for a in current if a.key == "occupation"] == ["doctor"]  # current untouched
    values = {a.value for a in await store.attributes_for(maya.id, current_only=False)}
    assert "receptionist" in values  # recorded as history
