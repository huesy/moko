"""xAI Grok text-to-speech adapter.

xAI exposes a dedicated `POST /v1/tts` endpoint (NOT the OpenAI `audio.speech`
shape), so this is a raw httpx call: JSON `{text, voice_id, language}` (plus an
optional `output_format` when we want something other than the default mp3) →
raw audio bytes. Auth mirrors the image adapter: an explicit `XAI_API_KEY`, or
the Grok *subscription* (OAuth) via a `refresh` hook, so one login covers chat +
image + video + voice. Documented shape (per Hermes' proven adapter); needs a
live key to fully validate — tests fake the seam.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

import httpx

from .base import AUDIO_MIME, SPEECH_TIMEOUT_S, XAI_BASE_URL, GeneratedAudio, TTSClient

_BASE = XAI_BASE_URL
_DEFAULT_VOICE = "eve"  # Hermes' default Grok voice
_DEFAULT_LANGUAGE = "en"
_MIME = AUDIO_MIME


class XaiTTSClient(TTSClient):
    """`api_key` for an explicit XAI_API_KEY, or `refresh` for the Grok subscription
    (OAuth) — refreshed before each call, exactly like the chat/image paths."""

    def __init__(
        self,
        model: str,
        api_key: str | None = None,
        *,
        voice: str = _DEFAULT_VOICE,
        language: str = _DEFAULT_LANGUAGE,
        refresh: Callable[[bool], Awaitable[str]] | None = None,
    ) -> None:
        self.model = model
        self._key = api_key or ""
        self._voice = voice or _DEFAULT_VOICE
        self._language = language or _DEFAULT_LANGUAGE
        self._refresh = refresh

    async def _bearer(self) -> str:
        if self._refresh is not None:
            self._key = await self._refresh(False)
        return self._key

    async def synthesize(self, text: str, *, fmt: str = "mp3") -> GeneratedAudio:
        bearer = await self._bearer()
        codec = fmt if fmt in _MIME else "mp3"
        payload: dict = {"text": text, "voice_id": self._voice, "language": self._language}
        if codec != "mp3":  # default is mp3; only override when asked for something else
            payload["output_format"] = {"codec": codec}
        async with httpx.AsyncClient(timeout=SPEECH_TIMEOUT_S) as http:
            r = await http.post(
                f"{_BASE}/tts",
                headers={"Authorization": f"Bearer {bearer}", "Content-Type": "application/json"},
                json=payload,
            )
            r.raise_for_status()
            return GeneratedAudio(r.content, _MIME[codec])
