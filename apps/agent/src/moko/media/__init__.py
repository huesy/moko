"""The media layer — one interface per capability, several providers.

`build_*_client` turns a configured `ModelChoice` into a ready client, or None when
that capability isn't set up (so it's simply, honestly *off* rather than faked):

  image   — `build_image_client`  → ImageClient   (text→image, self-portraits)
  video   — `build_video_client`  → VideoClient   (text/image→video)
  speech  — `build_tts_client`    → TTSClient     (Moko's voice)
  hearing — `build_stt_client`    → STTClient     (transcribe inbound voice notes)

Each capability is configured *independently* of the chat model (a text-only or
local brain can still have a cloud voice). xAI-backed capabilities prefer the Grok
*subscription* (OAuth) over a stored key — one `moko login` covers them all.
"""

from __future__ import annotations

import os
from collections.abc import Callable

from ..config import secrets
from ..config.config import Config, ModelChoice
from ..config.providers import MODALITY_DEFAULT_MODEL
from .base import (
    GeneratedAudio,
    GeneratedImage,
    GeneratedVideo,
    ImageClient,
    STTClient,
    Transcript,
    TTSClient,
    VideoClient,
)

__all__ = [
    "ImageClient", "GeneratedImage", "build_image_client",
    "VideoClient", "GeneratedVideo", "build_video_client",
    "TTSClient", "GeneratedAudio", "build_tts_client",
    "STTClient", "Transcript", "build_stt_client",
]

# Default model per provider, so config can name just the provider. These come from the
# provider/modality catalog (config.providers) — the single source of truth the setup
# wizard and `moko config` also read — so the builders can't drift out of step with it.
# Never a model id hard-coded into behaviour, only a sensible default the companion can
# override. An empty/absent default means the provider's endpoint has no model id to send
# (xAI /tts, /stt) or the adapter auto-selects it (xAI video text-vs-image backend).
_DEFAULT_IMAGE_MODEL = MODALITY_DEFAULT_MODEL["image"]
_OPENAI_TTS_MODEL = MODALITY_DEFAULT_MODEL["speech"]["openai"]
_OPENAI_STT_MODEL = MODALITY_DEFAULT_MODEL["hearing"]["openai"]


def _key(provider: str, *env: str) -> str | None:
    """The stored API key for a provider, falling back to environment variables."""
    found = secrets.get_api_key(provider)
    for var in env:
        found = found or os.environ.get(var)
    return found


def _openai(choice: ModelChoice, default: str, build: Callable):
    """Build an OpenAI-backed client (image/tts/stt), or None without a key. `build(model,
    key)` — mirrors `_xai` so each provider arm in the builders is a single line."""
    key = _key("openai", "OPENAI_API_KEY")
    if not key:
        return None
    return build(choice.model or default, key)


def _xai(choice: ModelChoice, model: str, build: Callable):
    """Build an xAI-backed client, preferring the Grok *subscription* (OAuth) — the
    same bearer the brain uses, no separate key — unless this slot explicitly chose an
    API key. `build(model, key, refresh)`; returns None if no credential is available.
    The OAuth token is the same `Bearer` against the same `api.x.ai/v1`, so one Grok
    login powers chat + image + video + voice (Hermes' proven approach)."""
    if choice.auth != "api_key":
        from ..llm.xai_auth import resolve_xai_oauth

        pair = resolve_xai_oauth()
        if pair is not None:
            token, refresh = pair
            return build(model, token, refresh)
    key = _key("xai", "XAI_API_KEY")
    if not key:
        return None
    return build(model, key, None)


def build_image_client(config: Config) -> ImageClient | None:
    """Construct an `ImageClient` from config + stored secrets, or None if image
    generation isn't configured (or its key is missing — the capability is then
    simply, honestly off)."""
    choice = config.model_for("image")
    if not choice.configured:
        return None
    provider, model = choice.provider, choice.model

    if provider == "openai":
        from .openai_image import OpenAIImageClient

        return _openai(choice, _DEFAULT_IMAGE_MODEL["openai"], OpenAIImageClient)

    if provider == "xai":  # Grok Imagine — OpenAI-ish generate, but its own size + edit shapes
        from .xai_image import XaiImageClient

        return _xai(
            choice, model or _DEFAULT_IMAGE_MODEL["xai"],
            lambda m, k, r: XaiImageClient(m, k, refresh=r),
        )

    if provider == "google":
        key = _key("google", "GOOGLE_API_KEY", "GEMINI_API_KEY")
        if not key:
            return None
        from .gemini_image import GeminiImageClient

        return GeminiImageClient(model or _DEFAULT_IMAGE_MODEL["google"], key)

    # Other providers (local engines, …) slot in here as adapters land.
    return None


def build_video_client(config: Config) -> VideoClient | None:
    """Construct a `VideoClient` (text/image→video), or None if video generation isn't
    configured or its credential is missing."""
    choice = config.model_for("video")
    if not choice.configured:
        return None
    if choice.provider == "xai":
        from .xai_video import XaiVideoClient

        # No default needed: XaiVideoClient picks the text/image backend from choice.model
        # (empty → it auto-selects).
        return _xai(choice, choice.model, lambda m, k, r: XaiVideoClient(m, k, refresh=r))
    return None


def build_tts_client(config: Config) -> TTSClient | None:
    """Construct a `TTSClient` (Moko's voice), or None if speech isn't configured or
    its credential is missing."""
    choice = config.model_for("speech")
    if not choice.configured:
        return None
    if choice.provider == "openai":
        from .openai_tts import OpenAITTSClient

        return _openai(choice, _OPENAI_TTS_MODEL, OpenAITTSClient)
    if choice.provider == "xai":
        from .xai_tts import XaiTTSClient

        return _xai(choice, choice.model, lambda m, k, r: XaiTTSClient(m, k, refresh=r))
    return None


def build_stt_client(config: Config) -> STTClient | None:
    """Construct an `STTClient` (Moko's hearing), or None if hearing isn't configured
    or its credential is missing."""
    choice = config.model_for("hearing")
    if not choice.configured:
        return None
    if choice.provider == "openai":
        from .openai_stt import OpenAISTTClient

        return _openai(choice, _OPENAI_STT_MODEL, OpenAISTTClient)
    if choice.provider == "xai":
        from .xai_stt import XaiSTTClient

        return _xai(choice, choice.model, lambda m, k, r: XaiSTTClient(m, k, refresh=r))
    return None
