"""Image-generation providers: routing + the per-provider wire shapes.

Since we can't hit the live APIs, these assert each adapter builds the *documented*
request (Gemini generateContent + x-goog-api-key; xAI aspect_ratio not size, JSON
edits endpoint; OpenAI url fallback) and parses the response — the executable
record of the research the adapters were written from.
"""

import base64
import json

import httpx

import moko.media.gemini_image as gem
import moko.media.openai_image as oa
import moko.media.xai_image as xa
from moko.config import secrets
from moko.config.config import Config, ModelChoice
from moko.media import build_image_client
from moko.media.gemini_image import GeminiImageClient
from moko.media.openai_image import OpenAIImageClient
from moko.media.xai_image import XaiImageClient


def _cfg(provider: str) -> Config:
    c = Config(companion_name="A", timezone="UTC", sleep_start="23:00",
               sleep_end="07:00", proactivity="moderate")
    c.models["image"] = ModelChoice(provider=provider, model="", auth="api_key")
    return c


def _mock_httpx(monkeypatch, module, handler):
    real = httpx.AsyncClient  # capture before patching (module.httpx is the httpx module)
    monkeypatch.setattr(
        module.httpx, "AsyncClient",
        lambda *a, **k: real(transport=httpx.MockTransport(handler)),
    )


def _b64(b: bytes) -> str:
    return base64.b64encode(b).decode("ascii")


# ── routing ───────────────────────────────────────────────────────────────────
def test_build_routes_each_provider(tmp_path, monkeypatch):
    monkeypatch.setenv("MOKO_HOME", str(tmp_path))
    for provider, cls, default in [
        ("openai", OpenAIImageClient, "gpt-image-1"),
        ("xai", XaiImageClient, "grok-imagine-image"),
        ("google", GeminiImageClient, "gemini-2.5-flash-image"),
    ]:
        secrets.set_api_key(provider, "k-" + provider)
        client = build_image_client(_cfg(provider))
        assert isinstance(client, cls) and client.model == default
    assert build_image_client(_cfg("none")) is None  # unconfigured → off


def test_build_returns_none_without_a_key(tmp_path, monkeypatch):
    monkeypatch.setenv("MOKO_HOME", str(tmp_path))
    assert build_image_client(_cfg("google")) is None  # configured but no key → off


# ── xAI Grok via OAuth/subscription (follows Hermes: OAuth preferred over key) ──
def _cfg_xai(auth: str) -> Config:
    c = Config(companion_name="A", timezone="UTC", sleep_start="23:00",
               sleep_end="07:00", proactivity="moderate")
    c.models["image"] = ModelChoice(provider="xai", model="", auth=auth)
    return c


def test_resolve_xai_oauth(tmp_path, monkeypatch):
    monkeypatch.setenv("MOKO_HOME", str(tmp_path))
    from moko.llm import xai_auth
    from moko.llm.xai_auth import resolve_xai_oauth

    xai_auth._reset_memo()
    assert resolve_xai_oauth() is None  # no subscription connected
    secrets.set_oauth_tokens("xai", {"access_token": "tok-1"})
    pair = resolve_xai_oauth()
    assert pair is not None and pair[0] == "tok-1"


def test_resolve_xai_oauth_is_shared_across_callers(tmp_path, monkeypatch):
    # The refresh token is single-use: the brain and every media builder must share ONE
    # closure/state, or the first refresh rotates the token out from under the others.
    monkeypatch.setenv("MOKO_HOME", str(tmp_path))
    from moko.llm import xai_auth
    from moko.llm.xai_auth import resolve_xai_oauth

    xai_auth._reset_memo()
    secrets.set_oauth_tokens("xai", {"access_token": "tok-shared"})
    brain = resolve_xai_oauth()  # the chat client's resolution (llm/__init__)
    media = resolve_xai_oauth()  # a media builder's resolution (media/__init__)
    assert brain is not None and media is not None
    assert brain[1] is media[1]  # the SAME refresh closure — one rotating state, memoized
    # A fresh `moko login` (a new, unrelated token) invalidates the memo and rebuilds.
    secrets.set_oauth_tokens("xai", {"access_token": "tok-after-relogin"})
    after = resolve_xai_oauth()
    assert after is not None and after[0] == "tok-after-relogin" and after[1] is not brain[1]


def test_build_xai_prefers_the_grok_subscription(tmp_path, monkeypatch):
    monkeypatch.setenv("MOKO_HOME", str(tmp_path))
    secrets.set_oauth_tokens("xai", {"access_token": "sub-tok"})  # `moko login` done
    client = build_image_client(_cfg_xai("oauth"))
    assert isinstance(client, XaiImageClient) and client._refresh is not None  # uses OAuth
    # No separate API key was needed — the subscription powers images too.


def test_build_xai_api_key_choice_forces_the_key_path(tmp_path, monkeypatch):
    monkeypatch.setenv("MOKO_HOME", str(tmp_path))
    secrets.set_oauth_tokens("xai", {"access_token": "sub-tok"})  # subscription present…
    secrets.set_api_key("xai", "key-1")
    client = build_image_client(_cfg_xai("api_key"))  # …but the slot explicitly chose a key
    assert isinstance(client, XaiImageClient) and client._refresh is None  # no OAuth refresh


async def test_xai_client_refreshes_bearer_before_the_call(monkeypatch):
    calls = []

    async def refresh(force=False):
        calls.append(force)
        return "fresh-tok"

    client = XaiImageClient("grok-imagine-image", refresh=refresh)
    captured = {}

    class _Datum:
        b64_json = _b64(b"X")

    class _Resp:
        data = [_Datum()]

    async def fake_generate(**kwargs):
        captured["key"] = client._client.api_key  # the bearer applied for this call
        return _Resp()

    client._client.images.generate = fake_generate
    await client.generate("a cat")
    assert calls == [False]  # refreshed (near-expiry check) right before the request
    assert captured["key"] == "fresh-tok"  # the fresh subscription token was used


# ── Gemini ────────────────────────────────────────────────────────────────────
async def test_gemini_generate_text_to_image(monkeypatch):
    seen = {}

    def handler(req):
        seen["url"] = str(req.url)
        seen["headers"] = {k.lower(): v for k, v in req.headers.items()}
        seen["body"] = json.loads(req.content)
        return httpx.Response(200, json={"candidates": [{"content": {"parts": [
            {"text": "here you go"},
            {"inlineData": {"mimeType": "image/png", "data": _b64(b"PNGDATA")}},
        ]}}]})

    _mock_httpx(monkeypatch, gem, handler)
    img = await GeminiImageClient("gemini-2.5-flash-image", "key123").generate(
        "a cat", size="1024x1536")
    assert img.data == b"PNGDATA" and img.mime == "image/png"
    assert "gemini-2.5-flash-image:generateContent" in seen["url"]
    assert seen["headers"]["x-goog-api-key"] == "key123"  # not Bearer
    cfg = seen["body"]["generationConfig"]
    assert cfg["responseModalities"] == ["TEXT", "IMAGE"]
    # The aspect must be sent explicitly, else Gemini follows the reference's shape.
    assert cfg["imageConfig"]["aspectRatio"] == "3:4"  # portrait
    assert seen["body"]["contents"][0]["parts"][-1]["text"] == "a cat"


async def test_gemini_reference_rides_as_inline_data(monkeypatch):
    seen = {}

    def handler(req):
        seen["body"] = json.loads(req.content)
        return httpx.Response(200, json={"candidates": [{"content": {"parts": [
            {"inlineData": {"mimeType": "image/png", "data": _b64(b"X")}}]}}]})

    _mock_httpx(monkeypatch, gem, handler)
    await GeminiImageClient("m", "k").generate("make me", reference=b"REF",
                                               reference_mime="image/png")
    parts = seen["body"]["contents"][0]["parts"]
    assert any("inlineData" in p for p in parts)  # the reference image was sent


# ── xAI / Grok Imagine ─────────────────────────────────────────────────────────
async def test_xai_generate_maps_size_to_aspect_and_omits_size(monkeypatch):
    captured = {}

    class _Datum:
        b64_json = _b64(b"IMG")

    class _Resp:
        data = [_Datum()]

    client = XaiImageClient("grok-imagine-image-quality", "k")

    async def fake_generate(**kwargs):
        captured.update(kwargs)
        return _Resp()

    client._client.images.generate = fake_generate
    img = await client.generate("a cat", size="1024x1536")  # portrait
    assert img.data == b"IMG"
    assert "size" not in captured  # xAI has no size param
    assert captured["extra_body"]["aspect_ratio"] == "3:4"  # natural portrait (not stretched 9:16)
    assert captured["response_format"] == "b64_json"


async def test_xai_reference_uses_json_edits_endpoint(monkeypatch):
    seen = {}

    def handler(req):
        seen["url"] = str(req.url)
        seen["auth"] = req.headers.get("authorization")
        seen["body"] = json.loads(req.content)
        return httpx.Response(200, json={"data": [{"b64_json": _b64(b"EDITED")}]})

    _mock_httpx(monkeypatch, xa, handler)
    img = await XaiImageClient("grok-imagine-image-quality", "kkk").generate(
        "as a sketch", size="1024x1536", reference=b"REF", reference_mime="image/png")
    assert img.data == b"EDITED"
    assert seen["url"].endswith("/v1/images/edits")  # JSON edits, not multipart
    assert seen["auth"] == "Bearer kkk"
    assert seen["body"]["image"]["type"] == "image_url"
    assert seen["body"]["image"]["url"].startswith("data:image/png;base64,")
    # The requested size rides into the edit too, as a natural portrait (3:4, not 9:16).
    assert seen["body"]["aspect_ratio"] == "3:4"


# ── OpenAI url fallback (serves DALL·E's hosted-URL responses) ──────────────────
async def test_openai_fetches_url_when_no_b64(monkeypatch):
    client = OpenAIImageClient("dall-e-3", "k")

    class _Datum:
        b64_json = None
        url = "https://images.example/x.png"

    class _Resp:
        data = [_Datum()]

    async def fake_generate(**kwargs):
        return _Resp()

    client._client.images.generate = fake_generate
    _mock_httpx(monkeypatch, oa, lambda req: httpx.Response(
        200, content=b"URLIMG", headers={"content-type": "image/png"}))
    img = await client.generate("a cat")
    assert img.data == b"URLIMG"
