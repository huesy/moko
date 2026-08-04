"""Voice (TTS/STT) + video media providers: routing + per-provider wire shapes.

Since we can't hit the live APIs, these assert each adapter builds the *documented*
request (xAI /tts, /stt, /videos/generations + poll; OpenAI /audio/speech,
/audio/transcriptions) and parses the response — the executable record of the
research (Hermes' proven adapters) the code was written from. Mirrors
test_image_providers.py.
"""

import json

import httpx

import moko.media.openai_stt as oss
import moko.media.openai_tts as ots
import moko.media.xai_stt as xss
import moko.media.xai_tts as xts
import moko.media.xai_video as xv
from moko.config import secrets
from moko.config.config import Config, ModelChoice
from moko.media import build_stt_client, build_tts_client, build_video_client
from moko.media.openai_stt import OpenAISTTClient
from moko.media.openai_tts import OpenAITTSClient
from moko.media.xai_stt import XaiSTTClient
from moko.media.xai_tts import XaiTTSClient
from moko.media.xai_video import XaiVideoClient


def _cfg(cap: str, provider: str, *, auth: str = "api_key", model: str = "") -> Config:
    c = Config(companion_name="A", timezone="UTC", sleep_start="23:00",
               sleep_end="07:00", proactivity="moderate")
    c.models[cap] = ModelChoice(provider=provider, model=model, auth=auth)
    return c


def _mock_httpx(monkeypatch, module, handler):
    real = httpx.AsyncClient  # capture before patching (module.httpx is the httpx module)
    monkeypatch.setattr(
        module.httpx, "AsyncClient",
        lambda *a, **k: real(transport=httpx.MockTransport(handler)),
    )


# ── routing ───────────────────────────────────────────────────────────────────
def test_build_tts_routes(tmp_path, monkeypatch):
    monkeypatch.setenv("MOKO_HOME", str(tmp_path))
    secrets.set_api_key("openai", "k")
    secrets.set_api_key("xai", "kx")
    assert isinstance(build_tts_client(_cfg("speech", "openai")), OpenAITTSClient)
    assert isinstance(build_tts_client(_cfg("speech", "xai")), XaiTTSClient)
    assert build_tts_client(_cfg("speech", "none")) is None  # unconfigured → off


def test_build_stt_routes(tmp_path, monkeypatch):
    monkeypatch.setenv("MOKO_HOME", str(tmp_path))
    secrets.set_api_key("openai", "k")
    secrets.set_api_key("xai", "kx")
    assert isinstance(build_stt_client(_cfg("hearing", "openai")), OpenAISTTClient)
    assert isinstance(build_stt_client(_cfg("hearing", "xai")), XaiSTTClient)
    assert build_stt_client(_cfg("hearing", "none")) is None


def test_build_video_routes(tmp_path, monkeypatch):
    monkeypatch.setenv("MOKO_HOME", str(tmp_path))
    secrets.set_api_key("xai", "kx")
    client = build_video_client(_cfg("video", "xai"))  # no model set → adapter auto-picks
    assert isinstance(client, XaiVideoClient)
    assert client._model_for(False) == "grok-imagine-video"  # text→video backend resolved
    assert client._model_for(True) == "grok-imagine-video-1.5-preview"  # image→video backend
    assert build_video_client(_cfg("video", "none")) is None


def test_build_returns_none_without_credential(tmp_path, monkeypatch):
    monkeypatch.setenv("MOKO_HOME", str(tmp_path))  # no keys, no OAuth
    assert build_tts_client(_cfg("speech", "openai")) is None
    assert build_stt_client(_cfg("hearing", "xai")) is None
    assert build_video_client(_cfg("video", "xai")) is None


# ── xAI subscription (OAuth) preferred over a key, same as image gen ────────────
def test_xai_voice_video_prefer_the_subscription(tmp_path, monkeypatch):
    monkeypatch.setenv("MOKO_HOME", str(tmp_path))
    secrets.set_oauth_tokens("xai", {"access_token": "sub-tok"})  # `moko login` done
    tts = build_tts_client(_cfg("speech", "xai", auth="oauth"))
    stt = build_stt_client(_cfg("hearing", "xai", auth="oauth"))
    vid = build_video_client(_cfg("video", "xai", auth="oauth"))
    assert tts._refresh is not None and stt._refresh is not None and vid._refresh is not None
    # No separate API key needed — the Grok subscription powers voice + video too.


def test_xai_api_key_choice_forces_the_key_path(tmp_path, monkeypatch):
    monkeypatch.setenv("MOKO_HOME", str(tmp_path))
    secrets.set_oauth_tokens("xai", {"access_token": "sub-tok"})  # subscription present…
    secrets.set_api_key("xai", "key-1")
    vid = build_video_client(_cfg("video", "xai", auth="api_key"))  # …but the slot chose a key
    assert isinstance(vid, XaiVideoClient) and vid._refresh is None  # no OAuth refresh


# ── xAI TTS (/v1/tts) ───────────────────────────────────────────────────────────
async def test_xai_tts_posts_to_tts_endpoint(monkeypatch):
    seen = {}

    def handler(req):
        seen["url"] = str(req.url)
        seen["auth"] = req.headers.get("authorization")
        seen["body"] = json.loads(req.content)
        return httpx.Response(200, content=b"AUDIO", headers={"content-type": "audio/mpeg"})

    _mock_httpx(monkeypatch, xts, handler)
    audio = await XaiTTSClient("", "kk").synthesize("hello", fmt="mp3")
    assert audio.data == b"AUDIO" and audio.mime == "audio/mpeg"
    assert seen["url"].endswith("/v1/tts")
    assert seen["auth"] == "Bearer kk"
    assert seen["body"]["text"] == "hello" and seen["body"]["voice_id"] == "eve"
    assert "output_format" not in seen["body"]  # mp3 is the default → minimal documented body


async def test_xai_tts_refreshes_bearer(monkeypatch):
    calls = []

    async def refresh(force=False):
        calls.append(force)
        return "fresh-tok"

    def handler(req):
        assert req.headers.get("authorization") == "Bearer fresh-tok"
        return httpx.Response(200, content=b"A", headers={"content-type": "audio/mpeg"})

    _mock_httpx(monkeypatch, xts, handler)
    await XaiTTSClient("", refresh=refresh).synthesize("hi")
    assert calls == [False]  # refreshed (near-expiry check) before the request


# ── OpenAI TTS (/v1/audio/speech) ────────────────────────────────────────────────
async def test_openai_tts_audio_speech(monkeypatch):
    seen = {}

    def handler(req):
        seen["url"] = str(req.url)
        seen["auth"] = req.headers.get("authorization")
        seen["body"] = json.loads(req.content)
        return httpx.Response(200, content=b"MP3", headers={"content-type": "audio/mpeg"})

    _mock_httpx(monkeypatch, ots, handler)
    audio = await OpenAITTSClient("gpt-4o-mini-tts", "k").synthesize("hi")
    assert audio.data == b"MP3"
    assert seen["url"].endswith("/v1/audio/speech")
    assert seen["auth"] == "Bearer k"
    assert seen["body"]["model"] == "gpt-4o-mini-tts" and seen["body"]["voice"] == "alloy"
    assert seen["body"]["response_format"] == "mp3" and seen["body"]["input"] == "hi"


async def test_openai_tts_maps_ogg_to_opus(monkeypatch):
    seen = {}

    def handler(req):
        seen["body"] = json.loads(req.content)
        return httpx.Response(200, content=b"x", headers={"content-type": "audio/ogg"})

    _mock_httpx(monkeypatch, ots, handler)
    audio = await OpenAITTSClient("m", "k").synthesize("hi", fmt="ogg")
    assert seen["body"]["response_format"] == "opus"  # OpenAI's name for the Ogg container
    assert audio.mime == "audio/ogg"


# ── xAI STT (/v1/stt) ─────────────────────────────────────────────────────────
async def test_xai_stt_multipart(monkeypatch):
    seen = {}

    def handler(req):
        seen["url"] = str(req.url)
        seen["auth"] = req.headers.get("authorization")
        seen["ct"] = req.headers.get("content-type", "")
        return httpx.Response(200, json={"text": "hello there", "language": "en"})

    _mock_httpx(monkeypatch, xss, handler)
    t = await XaiSTTClient("", "kk").transcribe(b"OGGDATA", mime="audio/ogg")
    assert t.text == "hello there" and t.language == "en"
    assert seen["url"].endswith("/v1/stt")
    assert seen["auth"] == "Bearer kk"
    assert seen["ct"].startswith("multipart/form-data")  # file upload, not JSON


async def test_stt_failure_degrades_to_empty_transcript(monkeypatch):
    _mock_httpx(monkeypatch, xss, lambda req: httpx.Response(500, text="nope"))
    t = await XaiSTTClient("", "k").transcribe(b"x")  # a bad listen never raises
    assert t.text == ""


# ── OpenAI STT (/v1/audio/transcriptions) ───────────────────────────────────────
async def test_openai_stt_transcriptions(monkeypatch):
    seen = {}

    def handler(req):
        seen["url"] = str(req.url)
        seen["ct"] = req.headers.get("content-type", "")
        return httpx.Response(200, json={"text": "transcribed"})

    _mock_httpx(monkeypatch, oss, handler)
    t = await OpenAISTTClient("whisper-1", "k").transcribe(b"AUD")
    assert t.text == "transcribed"
    assert seen["url"].endswith("/v1/audio/transcriptions")
    assert seen["ct"].startswith("multipart/form-data")


# ── xAI video (/v1/videos/generations → poll → fetch) ────────────────────────────
def _video_handler(seen: dict, frames_before_done: int = 2):
    """A MockTransport handler that walks xAI's async video lifecycle: submit →
    `frames_before_done` queued polls → done with a CDN URL → the CDN fetch."""
    def handler(req):
        path = req.url.path
        if path.endswith("/videos/generations"):
            seen["body"] = json.loads(req.content)
            return httpx.Response(200, json={"request_id": "rq1"})
        if "/videos/rq1" in path:
            seen["polls"] = seen.get("polls", 0) + 1
            if seen["polls"] < frames_before_done:
                return httpx.Response(200, json={"status": "queued"})
            return httpx.Response(
                200, json={"status": "done", "video": {"url": "https://cdn.x.ai/v.mp4"}})
        return httpx.Response(200, content=b"VIDEOBYTES", headers={"content-type": "video/mp4"})
    return handler


async def test_xai_video_submit_poll_fetch(monkeypatch):
    seen = {}
    _mock_httpx(monkeypatch, xv, _video_handler(seen))
    monkeypatch.setattr(xv, "_POLL_INTERVAL_S", 0)  # no real sleeps in the poll loop
    vid = await XaiVideoClient("grok-imagine-video", "kk").generate("a cat", aspect_ratio="9:16")
    assert vid.data == b"VIDEOBYTES" and vid.mime == "video/mp4"
    assert seen["body"]["model"] == "grok-imagine-video"  # text→video backend
    assert seen["body"]["aspect_ratio"] == "9:16" and seen["body"]["resolution"] == "720p"
    assert seen["polls"] >= 2  # actually polled until done


async def test_xai_video_image_to_video_uses_preview_model(monkeypatch):
    seen = {}
    _mock_httpx(monkeypatch, xv, _video_handler(seen, frames_before_done=1))
    monkeypatch.setattr(xv, "_POLL_INTERVAL_S", 0)
    await XaiVideoClient("grok-imagine-video", "k").generate(
        "animate this", reference=b"IMG", reference_mime="image/png")
    assert seen["body"]["model"] == "grok-imagine-video-1.5-preview"  # image→video backend
    assert seen["body"]["image"]["url"].startswith("data:image/png;base64,")


async def test_xai_video_raises_on_failed_status(monkeypatch):
    def handler(req):
        if req.url.path.endswith("/videos/generations"):
            return httpx.Response(200, json={"request_id": "r"})
        return httpx.Response(200, json={"status": "failed", "error": {"message": "bad prompt"}})

    _mock_httpx(monkeypatch, xv, handler)
    monkeypatch.setattr(xv, "_POLL_INTERVAL_S", 0)
    try:
        await XaiVideoClient("grok-imagine-video", "k").generate("x")
        raise AssertionError("expected a failure")
    except RuntimeError as exc:
        assert "bad prompt" in str(exc)  # the provider's message is surfaced, not swallowed
