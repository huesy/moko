"""M10 — image generation (the `media/` seam + the gated `generate_image` tool).

Vision is *understanding* an image (a chat capability); this is *making* one — a
separate, paid service behind its own provider config. The load-bearing bits: the
client is built only when configured, generation is gated as spend, a generated
image is saved as a sourced outbound artifact (Moko remembers what it made), and a
per-turn cap stops a prompt-injected blast.
"""

import pytest

from moko.agent import Brain
from moko.config.config import Config
from moko.config.paths import ensure_home, media_dir
from moko.llm.base import LLMResponse
from moko.media import build_image_client
from moko.media.base import GeneratedImage
from moko.store import Store
from moko.tools.consent import ALLOW, ASK

CFG = Config(companion_name="Jordan", timezone="Asia/Tokyo",
             sleep_start="23:00", sleep_end="07:00", proactivity="moderate")


class _SilentLLM:
    model = "fake"

    async def converse(self, system, messages, *, tools, execute, max_tokens=1024,
                       temperature=1.0, max_steps=6, **kw):
        return LLMResponse("ok")


class _FakeImageClient:
    model = "fake-image"

    def __init__(self, *, supports_reference=False):
        self.calls = []
        self.sizes = []
        self.supports_reference = supports_reference  # gate the reference like a real adapter

    async def generate(self, prompt, *, size="1024x1024", reference=None,
                       reference_mime="image/png"):
        self.calls.append({"prompt": prompt, "reference": reference, "size": size})
        self.sizes.append(size)
        return GeneratedImage(b"PNG:" + prompt.encode(), "image/png")


@pytest.fixture
async def store(tmp_path):
    s = await Store.open(tmp_path / "moko.db")
    try:
        yield s
    finally:
        await s.close()


def _brain_with_image(store, *, supports_reference=False):
    brain = Brain(store, _SilentLLM(), CFG)
    client = _FakeImageClient(supports_reference=supports_reference)
    brain.studio.image = client
    # Register the tools even though CFG didn't configure one (the fixture injects it).
    for tool in (brain.studio._make_generate_image_tool(),
                 brain.studio._make_self_portrait_tool(),
                 brain.studio._make_share_photo_tool()):
        brain._tools.setdefault(tool.spec.name, tool)
    return brain, client


def test_build_image_client_is_none_without_config():
    assert build_image_client(CFG) is None  # no image provider set → capability off


async def test_generate_saves_a_sourced_outbound_artifact(store):
    ensure_home()
    brain, client = _brain_with_image(store)

    out = await brain.studio._generate_image({"prompt": "a fluffy pink creature in a garden"})
    assert "I made the image" in out
    # The GENERAL channel: a normal image of the request — no Moko styling/aspect forced —
    # plus a no-stray-text instruction.
    assert "a fluffy pink creature in a garden" in client.calls[0]["prompt"]
    assert "cartoon" not in client.calls[0]["prompt"]  # no Moko styling forced
    assert client.calls[0]["size"] == "1024x1024"  # provider default, not Moko's portrait
    assert "Do not put any text" in client.calls[0]["prompt"]

    art = (await store.recent_media())[0]
    assert art.kind == "image" and art.direction == "out" and art.source_id
    assert (media_dir() / art.path).read_bytes().startswith(b"PNG:")
    # Moko remembers it made the image — recorded with the *clean* prompt, not the
    # internal no-text directive.
    assert art.description == "an image I made: a fluffy pink creature in a garden"
    assert any("creature" in h.content for h in await store.recall("creature garden"))


async def test_generate_keeps_text_when_expressly_asked(store):
    ensure_home()
    brain, client = _brain_with_image(store)
    await brain.studio._generate_image({"prompt": "a poster that says HELLO in big letters"})
    # The companion asked for words → no suppression instruction is added.
    assert "Do not put any text" not in client.calls[0]["prompt"]


async def test_generation_runs_without_asking_by_default(store):
    # Through the real consent gate: image generation does NOT ask (the companion's own
    # call, a cheap creative act) — but it's still a recorded, policy-overridable action.
    ensure_home()
    brain, _ = _brain_with_image(store)
    asked = []

    async def approve(payload, description, category, always_ok):
        asked.append(description)
        return "approve"

    brain._request_approval = approve
    out = await brain._run_tool("generate_image", {"prompt": "a sunset"})
    assert not asked  # no permission prompt
    assert "I made the image" in out  # ran straight through


async def test_generation_re_gatable_by_policy(store):
    # The owner can still put image gen behind the gate again with `moko trust image ask`.
    ensure_home()
    brain, _ = _brain_with_image(store)
    brain._tool_policy = {"image": "ask"}
    asked = []

    async def approve(payload, description, category, always_ok):
        asked.append(description)
        return "approve"

    brain._request_approval = approve
    out = await brain._run_tool("generate_image", {"prompt": "a sunset"})
    assert asked and "generate an image" in asked[0]  # policy re-gated it
    assert "I made the image" in out  # approved → produced


async def test_no_image_model_is_honest(store):
    brain = Brain(store, _SilentLLM(), CFG)  # CFG has no image provider → client None
    assert brain.studio.image is None
    assert "generate_image" not in brain._tools  # not offered when unconfigured
    out = await brain.studio._generate_image({"prompt": "x"})
    assert "no image model is set up" in out


async def test_per_turn_cap_stops_a_blast(store):
    ensure_home()
    brain, client = _brain_with_image(store)
    results = [await brain.studio._generate_image({"prompt": f"img {i}"}) for i in range(5)]
    made = [r for r in results if "I made the image" in r]
    assert len(made) == 3  # _MAX_IMAGES_PER_TURN
    assert "let's not overdo it" in results[-1]
    assert len(client.calls) == 3  # the cap stops the call, not just the reply


def test_gate_defaults():
    # Sanity on the consent floor used: sensitive → ASK unless trusted.
    from moko.tools.base import SENSITIVE
    from moko.tools.consent import decide
    assert decide("image", SENSITIVE, {}) == ASK
    assert decide("image", SENSITIVE, {"image": "allow"}) == ALLOW  # `moko trust image allow`


# ── self-portrait ─────────────────────────────────────────────────────────────
async def test_self_portrait_is_a_selfie_from_the_phone(store):
    # The default xAI path is text-to-image: the selfie is the picture the front camera
    # captures (a close-up of Moko), NOT a scene of Moko holding a phone.
    ensure_home()
    brain, client = _brain_with_image(store)  # supports_reference=False (xAI-like)
    await brain.studio._self_portrait({})  # no scenario → a plain selfie
    prompt = client.calls[0]["prompt"]
    assert "selfie" in prompt and "selfie angle" in prompt
    assert "NOT a third-person scene of Moko holding or using a phone" in prompt
    assert "do NOT draw any camera, phone, or device" in prompt  # the device must never appear
    assert client.calls[0]["reference"] is None  # text-only provider → no reference attached
    assert "thumb and three fingers" in prompt  # the character bible rode in
    assert "cartoon" in prompt and "NOT photorealistic" in prompt
    assert "exactly TWO short arms" in prompt  # anatomy pinned (no third selfie arm)
    assert "any arm reaching out to hold one" in prompt  # camera is the POV, not a 3rd arm
    assert "Do not put any text" in prompt
    assert client.calls[0]["size"] == "1024x1536"  # portrait (xAI maps it to 3:4)


async def test_self_portrait_expression_tracks_mood_and_scenario_rides_in(store):
    ensure_home()
    brain, client = _brain_with_image(store)
    # A happy mood → a smiling expression in the prompt.
    await store.add_thought("feeling", "I'm fond of this companion", valence=0.8)
    await brain.studio._self_portrait({"scenario": "at the window watching the rain"})
    prompt = client.calls[0]["prompt"]
    assert "window watching the rain" in prompt  # the scenario rides in
    assert "expression right now" in prompt and "smile" in prompt  # positive mood → smile


async def test_neutral_mood_selfie_still_smiles(store):
    # The bug fix: a neutral/unknown mood must NOT render a flat or frowny face — selfies
    # default to a warm, friendly smile unless Moko is genuinely low.
    ensure_home()
    brain, client = _brain_with_image(store)  # no thoughts → unknown valence
    await brain.studio._self_portrait({})
    assert "warm, friendly smile" in client.calls[0]["prompt"]


def test_time_of_day_buckets():
    from moko.media.prompts import time_of_day
    assert time_of_day(9)[0] == "morning"
    assert time_of_day(13)[0] == "midday"
    assert time_of_day(19)[0] == "evening"
    assert time_of_day(23)[0] == "night" and "lamplight" in time_of_day(23)[1]
    assert time_of_day(2)[0] == "night"


async def test_selfie_prompt_carries_scene_context(store):
    # The image is grounded in the moment — current date/time + a matching lighting cue.
    ensure_home()
    brain, client = _brain_with_image(store)
    await brain.studio._self_portrait({})
    prompt = client.calls[0]["prompt"]
    assert "Right now it's" in prompt and "should match" in prompt  # date/time + light + season


async def test_selfie_prompt_picks_up_the_weather_cue(store):
    # When weather is known, its scene cue rides into the selfie so a rainy day looks rainy.
    from moko.tools.weather import Weather

    ensure_home()
    brain, client = _brain_with_image(store)
    # The brain pins the turn's weather on the studio; do that directly here.
    brain.studio.begin_turn(Weather("Okayama", 14.0, "light rain", "soft rain, wet light", True))
    await brain.studio._self_portrait({})
    assert "soft rain, wet light" in client.calls[0]["prompt"]


async def test_low_mood_selfie_looks_down(store):
    ensure_home()
    brain, client = _brain_with_image(store)
    await store.add_thought("feeling", "a hard day", valence=-0.7)
    await brain.studio._self_portrait({})
    assert "downturned" in client.calls[0]["prompt"]  # genuinely low → a sadder face


async def test_image_failure_reports_the_real_reason(store):
    # A failed generation surfaces WHY (e.g. a 429 quota), so Moko can relay it instead of
    # a generic "it didn't work".
    import httpx

    ensure_home()
    brain, _ = _brain_with_image(store)

    class _Boom:
        model = "m"
        supports_reference = False

        async def generate(self, *a, **k):
            req = httpx.Request("POST", "https://x/y")
            resp = httpx.Response(429, request=req)
            raise httpx.HTTPStatusError("nope", request=req, response=resp)

    brain.studio.image = _Boom()
    out = await brain.studio._generate_image({"prompt": "a cat"})
    assert "429" in out and ("quota" in out or "rate limit" in out)
    out2 = await brain.studio._self_portrait({})
    assert "429" in out2  # the selfie path surfaces it too


async def test_self_portrait_uses_the_reference_when_the_provider_supports_it(store, tmp_path):
    # A reference-capable provider (Gemini/OpenAI) gets the reference image as a character
    # model sheet — and is told to compose a new picture in that cartoon style.
    ensure_home()
    brain, client = _brain_with_image(store, supports_reference=True)
    ref = tmp_path / "moko-ref.png"
    ref.write_bytes(b"REFERENCE-MOKO")
    brain.config.self_portrait_reference = str(ref)

    await brain.studio._self_portrait({"scenario": "sipping cocoa"})
    call = client.calls[0]
    assert call["reference"] == b"REFERENCE-MOKO"  # the locked character keeps it on-model
    assert "character model sheet" in call["prompt"]
    assert "brand-new picture" in call["prompt"]
    assert "cartoon" in call["prompt"] and "NOT photorealistic" in call["prompt"]


async def test_self_portrait_keeps_text_when_scenario_asks(store):
    ensure_home()
    brain, client = _brain_with_image(store)
    await brain.studio._self_portrait({"scenario": "holding a sign that says HELLO"})
    assert "Do not put any text" not in client.calls[0]["prompt"]  # text was requested

    art = (await store.recent_media())[0]
    assert art.direction == "out" and "self-portrait" in (art.description or "")
    # Moko remembers having pictured itself — a private thought.
    assert any("picture of myself" in t.body for t in await store.recent_thoughts(limit=5))


async def test_reference_capable_provider_falls_back_to_the_bundled_reference(store):
    # With no user-placed reference, a reference-capable provider uses the one bundled in the
    # repo — so Moko stays on-model even on a fresh machine.
    from moko.media.prompts import BUNDLED_REFERENCE

    ensure_home()
    brain, client = _brain_with_image(store, supports_reference=True)
    brain.config.self_portrait_reference = "/no/such/moko-ref.png"  # user path absent
    await brain.studio._self_portrait({})
    assert client.calls[0]["reference"] == BUNDLED_REFERENCE.read_bytes()  # bundled fallback


async def test_self_portrait_uses_the_editable_character_bible(store):
    # The text character description is config-driven, so it can be refined live; what's set
    # there is exactly what's prepended to the prompt on a text-only provider.
    ensure_home()
    brain, client = _brain_with_image(store)
    brain.config.image_character = "Moko is a teal cube with googly eyes"  # a silly override
    await brain.studio._self_portrait({})
    assert "teal cube with googly eyes" in client.calls[0]["prompt"]


async def test_self_portrait_runs_without_asking(store):
    ensure_home()
    brain, _ = _brain_with_image(store)
    asked = []

    async def approve(payload, description, category, always_ok):
        asked.append(description)
        return "approve"

    brain._request_approval = approve
    out = await brain._run_tool("self_portrait", {"scenario": "in the garden"})
    assert not asked  # no permission prompt for a selfie either
    assert "I made a picture of myself" in out


async def test_self_portrait_requests_portrait_orientation(store):
    ensure_home()
    brain, client = _brain_with_image(store)
    brain.config.self_portrait_reference = "/no/ref.png"
    await brain.studio._self_portrait({"scenario": "napping"})
    assert client.sizes[-1] == "1024x1536"  # portrait, "like it came from a phone"


async def test_generation_failure_announces_a_failure_event(store):
    # A failed generation surfaces WHY on the feed, not just silence.
    import asyncio

    from moko import events

    ensure_home()
    brain, _ = _brain_with_image(store)

    class _Boom:
        model = "fake-image"
        supports_reference = False

        async def generate(self, *a, **k):
            raise RuntimeError("the service hit its rate limit (HTTP 429)")

    brain.studio.image = _Boom()
    sub = events.subscribe(backfill=False)
    try:
        await brain.studio._generate_image({"prompt": "a sunset"})
        ev = await asyncio.wait_for(sub.queue.get(), timeout=1)
    finally:
        sub.close()
    assert ev["type"] == "failure" and ev["what"] == "media" and ev["media_kind"] == "image"
    assert "rate limit" in ev["reason"] or "429" in ev["reason"]


async def test_video_generation_failure_announces_a_failure_event(store):
    import asyncio

    from moko import events

    ensure_home()
    brain, _ = _brain_with_image(store)

    class _Boom:
        model = "fake-video"

        async def generate(self, *a, **k):
            raise RuntimeError("the service timed out")

    brain.studio.video = _Boom()
    sub = events.subscribe(backfill=False)
    try:
        await brain.studio._generate_video({"prompt": "a clip"})
        ev = await asyncio.wait_for(sub.queue.get(), timeout=1)
    finally:
        sub.close()
    assert ev["type"] == "failure" and ev["media_kind"] == "video"


async def test_generation_announces_a_media_event_with_the_prompt(store):
    # Every generated picture hits the feed with the EXACT prompt used + a thumbnail id.
    import asyncio

    from moko import events

    ensure_home()
    brain, _ = _brain_with_image(store)
    sub = events.subscribe(backfill=False)
    try:
        await brain.studio._self_portrait({"scenario": "at my desk"})
        ev = await asyncio.wait_for(sub.queue.get(), timeout=1)
    finally:
        sub.close()
    assert ev["type"] == "media.generated" and ev["media_kind"] == "image"
    assert "selfie" in ev["gen_prompt"]  # the verbatim generation prompt rode along
    assert isinstance(ev["id"], str)  # deep-links to a thumbnail


async def test_pictures_are_tagged_with_their_world(store):
    # Selfies + Moko's own photos belong to Moko's world; a general render is the companion's.
    from moko.store import COMPANION_WORLD, MOKO_WORLD

    ensure_home()
    brain, _ = _brain_with_image(store)
    await brain.studio._self_portrait({})
    await brain.studio._share_photo({"subject": "the garden"})
    await brain.studio._generate_image({"prompt": "a banner for a sale"})

    arts = await store.recent_media(limit=9)
    worlds = {(m.description or m.caption or ""): m.world_id for m in arts}
    assert worlds[next(d for d in worlds if "self-portrait" in d)] == MOKO_WORLD
    assert worlds[next(d for d in worlds if "photo I took" in d)] == MOKO_WORLD
    assert worlds[next(d for d in worlds if "image I made" in d)] == COMPANION_WORLD


# ── share_photo: a one-off phone photo of a scene Moko wants to show ────────────
async def test_share_photo_makes_a_photo(store):
    ensure_home()
    brain, client = _brain_with_image(store)
    out = await brain.studio._share_photo({"subject": "a cozy reading nook by a rainy window"})
    assert "send_attachment" in out
    call = client.calls[0]
    assert call["reference"] is None  # NOT a selfie → no character sheet (Moko won't appear)
    assert call["size"] == "1024x1536"  # portrait, taken-on-a-phone
    assert "cozy reading nook" in call["prompt"]
    assert "Moko's world" in call["prompt"]  # the shared cozy storybook aesthetic
    assert "small, round, soft, fluffy" not in call["prompt"]  # not describing Moko
    assert "Do not put any text" in call["prompt"]
    art = (await store.recent_media())[0]
    assert art.kind == "image" and art.direction == "out"
    assert "a photo I took" in (art.description or "")


async def test_share_photo_runs_without_asking(store):
    ensure_home()
    brain, _ = _brain_with_image(store)
    asked = []

    async def approve(payload, description, category, always_ok):
        asked.append(description)
        return "approve"

    brain._request_approval = approve
    out = await brain._run_tool("share_photo", {"subject": "a sunset over the hills"})
    assert not asked  # ungated, like the other image tools
    assert "send_attachment" in out


# ── proactive "show": Moko shares a photo of something from its world, unprompted ──
async def test_proactive_show_offers_and_attaches_a_photo(store):
    ensure_home()
    brain, client = _brain_with_image(store)
    # A shareable thought (a "discovery") is what makes the show candidate appear.
    await store.add_thought(
        "observation", "the autumn light in the courtyard is lovely",
        valence=0.6, shareability="shareable",
    )
    seen_tokens = []

    async def fake_consult(now, candidates):
        seen_tokens.extend(c["token"] for c in candidates)
        return {
            "act": True, "candidate": "show", "message": "look what I found today!",
            "photo_subject": "the autumn light in the courtyard",
        }

    brain._consult_outreach = fake_consult
    from moko.clock import now_utc
    result = await brain.consider_outreach(now_utc(), force=True)  # force skips the fuses
    assert result is not None
    message, proposal = result
    assert "show" in seen_tokens  # the candidate was offered (shareable thought + image model)
    assert message == "look what I found today!"
    assert proposal.kind == "show" and proposal.photo_path  # a photo was generated + attached
    # It's a photo Moko took (no character reference), saved as a sourced outbound artifact.
    assert client.calls[-1]["reference"] is None
    art = (await store.recent_media(limit=1))[0]
    assert "a photo I took" in (art.description or "")


async def test_no_show_candidate_without_an_image_model(store):
    # No image client → Moko can't take photos, so the "show" option isn't even offered.
    brain = Brain(store, _SilentLLM(), CFG)
    assert brain.studio.image is None
    await store.add_thought("observation", "a lovely thing", valence=0.6, shareability="shareable")
    seen = []

    async def fake_consult(now, candidates):
        seen.extend(c["token"] for c in candidates)
        return {"act": False}

    brain._consult_outreach = fake_consult
    from moko.clock import now_utc
    await brain.consider_outreach(now_utc(), force=True)
    assert "show" not in seen  # not offered when Moko has no camera


async def test_reference_path_drops_conflicting_positive_shading(store):
    # With a reference, its own shading governs — we must not also assert "flat cartoon
    # shading" (that fought "match the reference's shading"). Only the anti-photo guard stays.
    ensure_home()
    brain, client = _brain_with_image(store, supports_reference=True)
    await brain.studio._self_portrait({})
    prompt = client.calls[0]["prompt"]
    assert "NOT photorealistic" in prompt  # the guard remains
    assert "flat cartoon shading" not in prompt  # the conflicting positive restatement is gone


async def test_no_reference_path_keeps_positive_style(store):
    ensure_home()
    brain, client = _brain_with_image(store)  # supports_reference=False → text-bible path
    await brain.studio._self_portrait({})
    assert "flat cartoon shading" in client.calls[0]["prompt"]  # positive style needed here
