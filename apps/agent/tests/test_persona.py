from moko.agent.persona import _MAX_ATTRS, _MAX_INNER_LIFE, _MAX_RELS, system_prompt
from moko.config.config import Config
from moko.skills import load_skills
from moko.store import GraphSlice, Thought
from moko.store.models import Attribute, Relationship, Subject

SKILLS = load_skills()  # the built-in core skills


def _cfg() -> Config:
    return Config(
        companion_name="Jordan",  # fictional test companion
        timezone="Asia/Tokyo",
        sleep_start="23:00",
        sleep_end="07:00",
        proactivity="moderate",
    )


def _attr(eid: str, key: str, value: str) -> Attribute:
    return Attribute("a", eid, key, value, "string", None, None, "s1", 1.0, "t")


def _rel(rid: str, to_id: str) -> Relationship:
    return Relationship(rid, "c", to_id, "knows", None, None, True, "s", 1.0, "t")


def test_persona_names_companion_and_self():
    sys = system_prompt(_cfg(), GraphSlice(), now="Monday", skills=SKILLS)
    assert "Jordan" in sys.stable
    assert "Moko" in sys.stable


def test_skills_carry_behaviour_into_the_stable_prefix():
    # Curiosity/recording guidance now lives in skills, injected into the
    # cached prefix — not hard-coded in the persona string.
    stable = system_prompt(_cfg(), GraphSlice(), now="Monday", skills=SKILLS).stable
    assert "follow-up" in stable.lower()  # curiosity skill
    assert "supersedes" in stable.lower()  # remembering skill


def test_no_skills_still_produces_a_valid_prompt():
    sys = system_prompt(_cfg(), GraphSlice(), now="Monday")
    assert "Moko" in sys.stable
    assert "# Your skills" not in sys.stable


def test_voice_scaffold_only_for_weak_brains():
    # Capable brains get no voice block — the prefix is byte-identical, zero extra
    # tokens; a weak brain gets a purely-additive note appended.
    capable = system_prompt(_cfg(), GraphSlice(), now="Monday", skills=SKILLS).stable
    weak = system_prompt(
        _cfg(), GraphSlice(), now="Monday", skills=SKILLS, needs_scaffolding=True
    ).stable
    assert "On voice:" not in capable
    assert "On voice:" in weak
    assert weak.startswith(capable)  # additive only — nothing in the shared prefix changed


def test_known_facts_render_in_the_volatile_sketch_not_the_prefix():
    # A distinctive name that can't appear in any skill's example text.
    subject = Subject("e1", "person", "Quennell", 1.0, "", "t")
    graph = GraphSlice(subjects=[subject], attributes={"e1": [_attr("e1", "city", "Reykjavik")]})
    sys = system_prompt(_cfg(), graph, now="Monday", skills=SKILLS)
    assert "Quennell" in sys.volatile and "Reykjavik" in sys.volatile
    assert "Quennell" not in sys.stable  # the sketch is NOT in the cached prefix


def test_clock_is_volatile_not_stable():
    sys = system_prompt(_cfg(), GraphSlice(), now="Monday 14:30", skills=SKILLS)
    assert "14:30" not in sys.stable  # cached prefix excludes the clock
    assert "14:30" in sys.volatile


def test_open_questions_block_is_volatile_numbered_and_optional():
    qs = [(1, "identity of Sam (person)"), (2, "Biscuit's age")]
    sys = system_prompt(_cfg(), GraphSlice(), now="Monday", skills=SKILLS, open_questions=qs)
    # Numbered + present in the (uncached) tail, so the model can weave one in AND
    # mark answered ones in `resolved_gaps`.
    assert "1. identity of Sam (person)" in sys.volatile
    assert "2. Biscuit's age" in sys.volatile
    assert "resolved_gaps" in sys.volatile
    assert "identity of Sam (person)" not in sys.stable  # never in the cached prefix

    # No open questions → no block at all.
    plain = system_prompt(_cfg(), GraphSlice(), now="Monday", skills=SKILLS)
    assert "Open questions" not in plain.volatile


def test_conversation_framing_is_reply_only_and_pins_to_the_latest_message():
    # The reply path (conversation=True) frames the transcript as reference and pins the
    # turn to the latest message — so Moko can't re-record a prior turn (the dup-reminder
    # bug). Other paths (a single-message consult) must NOT carry it.
    reply = system_prompt(_cfg(), GraphSlice(), now="Monday", skills=SKILLS, conversation=True)
    assert "most recent message" in reply.volatile and "never re-record" in reply.volatile
    other = system_prompt(_cfg(), GraphSlice(), now="Monday", skills=SKILLS)
    assert "most recent message" not in other.volatile  # default off


def _thought(body: str, kind: str = "feeling") -> Thought:
    return Thought("t", kind, body, None, "private", 1.0, 0.5, "t")


def test_inner_life_rides_in_the_volatile_tail():
    thoughts = [_thought("I've grown fond of this companion", "opinion")]
    sys = system_prompt(_cfg(), GraphSlice(), now="Monday", skills=SKILLS, inner_life=thoughts)
    assert "grown fond of this companion" in sys.volatile  # mood carried over
    assert "grown fond of this companion" not in sys.stable  # never in cached prefix

    # No thoughts → no inner-life block at all.
    plain = system_prompt(_cfg(), GraphSlice(), now="Monday", skills=SKILLS)
    assert "recent inner life" not in plain.volatile.lower()


def test_inner_life_is_capped():
    thoughts = [_thought(f"thought number {i}") for i in range(_MAX_INNER_LIFE + 5)]
    vol = system_prompt(
        _cfg(), GraphSlice(), now="Monday", skills=SKILLS, inner_life=thoughts
    ).volatile
    assert "thought number 0" in vol
    assert f"thought number {_MAX_INNER_LIFE - 1}" in vol
    assert f"thought number {_MAX_INNER_LIFE}" not in vol  # overflow dropped


def test_inner_life_labels_shareability_so_the_boundary_is_legible():
    # The honesty boundary must be legible at speak-time: a private thought is
    # carried (it colors tone) but never marked voiceable; a shareable one is.
    private = Thought("t1", "opinion", "I worry they overwork", -0.3, "private", 1.0, 0.5, "t")
    sharee = Thought("t2", "feeling", "glad we talked today", 0.6, "shareable", 1.0, 0.5, "t")
    vol = system_prompt(
        _cfg(), GraphSlice(), now="Monday", skills=SKILLS, inner_life=[private, sharee]
    ).volatile
    assert "I worry they overwork" in vol  # the private thought still shapes the turn
    private_line = next(ln for ln in vol.splitlines() if "overwork" in ln)
    assert "shareable" not in private_line and "on_ask" not in private_line  # not voiceable
    share_line = next(ln for ln in vol.splitlines() if "glad we talked" in ln)
    assert "shareable" in share_line  # the voiceable one is flagged
    # The bare label notes the rule; the full explanation lives in the cached honesty skill.
    assert "private unless" in vol.lower()


def test_orientation_core_is_capped():
    # A companion with far more facts/links than the caps must not blow up the
    # volatile tail — the sketch is bounded, the tools reach the rest.
    companion = Subject("c", "person", "Jordan", 1.0, "", "t")
    others = [Subject(f"o{i}", "person", f"P{i}", 1.0, "", "t") for i in range(_MAX_RELS + 10)]
    attrs = [_attr("c", f"key_{i}", f"val_{i}") for i in range(_MAX_ATTRS + 10)]
    rels = [_rel(f"r{i}", o.id) for i, o in enumerate(others)]
    graph = GraphSlice(subjects=[companion, *others], attributes={"c": attrs}, relationships=rels)
    vol = system_prompt(_cfg(), graph, now="Monday", skills=SKILLS).volatile

    assert "key_0" in vol and f"key_{_MAX_ATTRS - 1}" in vol  # first N attrs shown
    assert f"key_{_MAX_ATTRS}" not in vol  # the overflow attr is dropped
    assert vol.count("knows") == _MAX_RELS  # relationships capped
