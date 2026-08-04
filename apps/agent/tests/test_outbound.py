"""M10 — outbound attachments (the gated `send_attachment` tool + channel upload).

The load-bearing property is the gate: a file from Moko's own media folder goes
straight to the companion, but a file from anywhere else on disk asks first —
sending an arbitrary local file off-box is exfil-class (safety floor #3). These
prove the gate fires exactly where it should, the send records an outbound
sourced artifact, and failures are honest.
"""

import httpx
import pytest

from moko.agent import Brain
from moko.config.config import Config
from moko.config.paths import ensure_home, media_dir
from moko.llm.base import LLMResponse
from moko.store import Store
from moko.tools.consent import ALLOW, ASK

CFG = Config(companion_name="Jordan", timezone="Asia/Tokyo",
             sleep_start="23:00", sleep_end="07:00", proactivity="moderate")


class _SilentLLM:
    model = "fake"

    async def converse(self, system, messages, *, tools, execute, max_tokens=1024,
                       temperature=1.0, max_steps=6, **kw):
        return LLMResponse("ok")


@pytest.fixture
async def store(tmp_path):
    s = await Store.open(tmp_path / "moko.db")
    try:
        yield s
    finally:
        await s.close()


def _brain_with_send(store):
    """A brain whose channel send path is a stub that captures what was sent."""
    sent = []

    async def send_file(*, data, filename, caption, kind):
        sent.append({"data": data, "filename": filename, "caption": caption, "kind": kind})

    brain = Brain(store, _SilentLLM(), CFG)
    brain._send_file = send_file
    brain._reply_channel = "telegram"
    return brain, sent


def test_gate_is_free_inside_media_dir_and_asks_outside(store, tmp_path):
    ensure_home()
    brain, _ = _brain_with_send(store)
    # A file under Moko's own media folder → free.
    inside = media_dir() / "mine.png"
    inside.write_bytes(b"PNG")
    assert brain._classify_send({"path": str(inside)}, {}).verdict == ALLOW
    # A file anywhere else → ask (and it's not freeable, so it always asks).
    outside = tmp_path / "elsewhere.pdf"
    outside.write_bytes(b"PDF")
    decision = brain._classify_send({"path": str(outside)}, {})
    assert decision.verdict == ASK and decision.key == "send_file"


async def test_send_announces_a_media_sent_event_with_a_thumbnail_id(store):
    import asyncio

    from moko import events

    ensure_home()
    brain, _ = _brain_with_send(store)
    f = media_dir() / "report.png"
    f.write_bytes(b"PNGDATA")
    sub = events.subscribe(backfill=False)
    try:
        await brain._send_attachment({"path": str(f), "caption": "here you go"})
        ev = await asyncio.wait_for(sub.queue.get(), timeout=1)
    finally:
        sub.close()
    assert ev["type"] == "media.sent" and ev["media_kind"] == "image"
    assert ev["caption"] == "here you go" and isinstance(ev["id"], str)  # id → thumbnail


async def test_send_records_outbound_sourced_artifact_and_sends_bytes(store):
    ensure_home()
    brain, sent = _brain_with_send(store)
    f = media_dir() / "report.png"
    f.write_bytes(b"PNGDATA")

    result = await brain._send_attachment({"path": str(f), "caption": "here you go"})
    assert "Sent report.png" in result
    # The channel got the actual bytes …
    assert sent and sent[0]["data"] == b"PNGDATA" and sent[0]["kind"] == "image"
    # … and Moko recorded an outbound, sourced artifact (it remembers what it sent).
    recent = await store.recent_media()
    assert len(recent) == 1 and recent[0].direction == "out"
    assert recent[0].source_id  # provenance, even outbound


async def test_send_expands_a_tilde_path(store, tmp_path, monkeypatch):
    # The model often writes '~/.moko/media/<file>'; without expansion that's treated as
    # relative and joined under media_dir into a bogus path (the cause of failed sends).
    ensure_home()
    monkeypatch.setenv("HOME", str(tmp_path))  # so ~ resolves into the test sandbox
    brain, sent = _brain_with_send(store)
    (tmp_path / "pic.png").write_bytes(b"PNGDATA")
    result = await brain._send_attachment({"path": "~/pic.png"})
    assert "Sent pic.png" in result and sent and sent[0]["data"] == b"PNGDATA"


async def test_send_does_not_double_record_a_generated_artifact(store):
    # A file Moko generated is recorded at creation; sending it must not record it again.
    from moko.media.base import GeneratedImage

    ensure_home()
    brain, sent = _brain_with_send(store)

    class _Img:
        model = "m"
        supports_reference = False

        async def generate(self, prompt, *, size="1024x1024", reference=None,
                           reference_mime="image/png"):
            return GeneratedImage(b"GEN-IMG", "image/png")

    brain.studio.image = _Img()
    made = await brain.studio._generate_image({"prompt": "a kite"})  # → 1 media row
    art_path = made.split("saved as ")[1].split(")")[0]
    await brain._send_attachment({"path": art_path})  # send it → must NOT add a 2nd row
    recent = await store.recent_media(limit=10)
    assert len(recent) == 1  # the generated artifact, recorded exactly once
    assert sent and sent[0]["data"] == b"GEN-IMG"


async def test_outside_file_routes_through_the_consent_gate(store, tmp_path):
    # Through the real gate: an outside file must trigger the approval path, not run free.
    ensure_home()
    brain, sent = _brain_with_send(store)
    asked = []

    async def approve(payload, description, category, always_ok):
        asked.append({"description": description, "always_ok": always_ok})
        return "approve"

    brain._request_approval = approve
    outside = tmp_path / "doc.pdf"
    outside.write_bytes(b"PDF")

    out = await brain._run_tool("send_attachment", {"path": str(outside)})
    assert asked and "doc.pdf" in asked[0]["description"]  # the gate fired
    assert asked[0]["always_ok"] is False  # exfil-class → never blanket-trusted
    assert sent and sent[0]["data"] == b"PDF"  # approved → actually sent
    assert "Sent doc.pdf" in out


async def test_media_dir_file_skips_the_gate(store):
    ensure_home()
    brain, sent = _brain_with_send(store)
    asked = []

    async def approve(payload, description, category, always_ok):
        asked.append(description)
        return "approve"

    brain._request_approval = approve
    f = media_dir() / "ok.png"
    f.write_bytes(b"X")
    await brain._run_tool("send_attachment", {"path": str(f)})
    assert asked == []  # free path → no approval prompt
    assert sent  # sent straight through


async def test_missing_file_and_no_channel_are_honest(store):
    ensure_home()
    brain, _ = _brain_with_send(store)
    miss = await brain._send_attachment({"path": str(media_dir() / "nope.png")})
    assert "couldn't find" in miss.lower()

    brain._send_file = None  # a channel that can't send
    f = media_dir() / "real.png"
    f.write_bytes(b"X")
    out = await brain._send_attachment({"path": str(f)})
    assert "can't send files" in out.lower()


# ── the Telegram upload itself ────────────────────────────────────────────────
async def test_telegram_send_file_uploads_photo_as_sendphoto(tmp_path):
    from moko.channels.telegram import TelegramChannel

    seen = {}

    def handler(req):
        seen["path"] = req.url.path
        seen["body"] = req.content  # multipart bytes
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 1}})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    ch = TelegramChannel("tok", "42", client=client, state_path=tmp_path / "tg.json")
    assert ch.can_send_files is True
    await ch.send_file(b"IMG", filename="a.png", caption="hi", kind="image")
    await client.aclose()
    assert "sendPhoto" in seen["path"]  # an image previews inline
    assert b"a.png" in seen["body"]  # the filename rode in the multipart body


# ── don't echo an attachment's caption in the reply ─────────────────────────────
async def _send_capture(store):
    sent = []

    async def send_file(*, data, filename, caption, kind):
        sent.append(caption)

    (media_dir() / "view.png").write_bytes(b"PNG")
    return store, sent, send_file


class _AttachThenSayLLM:
    """Sends a captioned attachment, then returns `reply` as the turn's words."""

    model = "fake"

    def __init__(self, caption, reply):
        self.caption, self.reply = caption, reply

    async def converse(self, system, messages, *, tools, execute, **kw):
        await execute("send_attachment", {"path": str(media_dir() / "view.png"),
                                          "caption": self.caption})
        return LLMResponse(self.reply)


async def test_reply_that_just_restates_the_caption_is_suppressed(store):
    ensure_home()
    _, sent, send_file = await _send_capture(store)
    llm = _AttachThenSayLLM("A sunny view over Okayama today, blue skies",
                            "A sunny view over Okayama today — blue skies!")
    turn = await Brain(store, llm, CFG).respond("show me outside", send_file=send_file)
    assert sent == ["A sunny view over Okayama today, blue skies"]  # captioned image went out
    assert turn.reply == ""  # the echoing reply was dropped — no reading it twice


async def test_genuinely_new_followup_is_kept(store):
    ensure_home()
    _, sent, send_file = await _send_capture(store)
    llm = _AttachThenSayLLM("A sunny view over Okayama today",
                            "Anyway — back to debugging that migration script with you!")
    turn = await Brain(store, llm, CFG).respond("show me outside", send_file=send_file)
    assert sent  # the image still went out
    assert "migration script" in turn.reply  # a real follow-up is preserved


async def test_uncaptioned_attachment_does_not_false_apologize(store):
    # The HIGH bug: a file sent with NO caption and no trailing text used to fall through
    # to "Sorry — I lost my thread" even though the file went out. Now it's silent.
    ensure_home()
    _, sent, send_file = await _send_capture(store)

    class _LLM:
        model = "fake"

        async def converse(self, system, messages, *, tools, execute, **kw):
            await execute("send_attachment", {"path": str(media_dir() / "view.png")})  # no caption
            return LLMResponse("")  # and no words after

    turn = await Brain(store, _LLM(), CFG).respond("send it", send_file=send_file)
    assert sent  # the file went out
    assert turn.reply == ""  # no false apology — the attachment is its own message


# ── daemonless `moko outreach` must send photos + record the sent text ──────────
class _CaptureChannel:
    """A file-capable channel that records what it sent — for the CLI fallback path."""

    name = "telegram"
    can_send_files = True

    def __init__(self):
        self.sent: list[str] = []
        self.files: list[dict] = []

    async def send(self, text):
        self.sent.append(text)

    async def send_file(self, data, *, filename, caption=None, kind="document"):
        self.files.append({"data": data, "filename": filename, "caption": caption, "kind": kind})


async def test_cli_outreach_sends_a_show_photo_and_records_text(store):
    # The daemonless fallback used to do a bare channel.send(text): it dropped a "show"
    # outreach's photo entirely and never passed text= to record_outreach (so the next
    # consult couldn't see the message and re-sent it). Now it sends the captioned image
    # and records the text — matching the daemon's gateway._deliver_outreach.
    from moko.agent.brain import OutreachProposal
    from moko.cli.outreach import _deliver

    ensure_home()
    (media_dir() / "world.png").write_bytes(b"PNGDATA")

    recorded = {}

    async def record_outreach(proposal, channel_name, *, text=""):
        recorded["channel"] = channel_name
        recorded["text"] = text

    brain = Brain(store, _SilentLLM(), CFG)
    brain.record_outreach = record_outreach  # capture the bookkeeping call
    ch = _CaptureChannel()
    proposal = OutreachProposal(
        "show", "the courtyard light", counts_to_budget=True, photo_path="world.png"
    )

    await _deliver(brain, ch, "look what I found today!", proposal)

    assert ch.sent == []  # not a plain-text send — it's a captioned image
    assert len(ch.files) == 1
    assert ch.files[0]["data"] == b"PNGDATA"
    assert ch.files[0]["caption"] == "look what I found today!"
    assert ch.files[0]["kind"] == "image"
    assert recorded["text"] == "look what I found today!"  # text= threaded through


async def test_cli_outreach_falls_back_to_text_when_photo_is_missing(store):
    # A "show" proposal whose photo can't be read (or a plain outreach with none) still
    # delivers the message as text and records it — never a silent drop.
    from moko.agent.brain import OutreachProposal
    from moko.cli.outreach import _deliver

    ensure_home()
    recorded = {}

    async def record_outreach(proposal, channel_name, *, text=""):
        recorded["text"] = text

    brain = Brain(store, _SilentLLM(), CFG)
    brain.record_outreach = record_outreach
    ch = _CaptureChannel()
    proposal = OutreachProposal(
        "show", "a thing", counts_to_budget=True, photo_path="does-not-exist.png"
    )

    await _deliver(brain, ch, "a warm hello", proposal)

    assert ch.files == []  # the unreadable photo → no upload
    assert ch.sent == ["a warm hello"]  # degraded to text, message preserved
    assert recorded["text"] == "a warm hello"  # still recorded
