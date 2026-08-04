"""OpenAI text-to-speech adapter.

`POST /v1/audio/speech` with `{model, voice, input, response_format}` → raw audio
bytes. Raw httpx (not the SDK's binary-response object) so the seam is uniform and
faked the same way in tests. Default `gpt-4o-mini-tts` / voice `alloy`.
"""

from __future__ import annotations

import httpx

from .base import AUDIO_MIME, SPEECH_TIMEOUT_S, GeneratedAudio, TTSClient

_BASE = "https://api.openai.com/v1"
_DEFAULT_VOICE = "alloy"
# OpenAI calls the Ogg container "opus"; everything else maps 1:1.
_FORMAT = {"mp3": "mp3", "wav": "wav", "ogg": "opus"}
_MIME = AUDIO_MIME


class OpenAITTSClient(TTSClient):
    def __init__(
        self, model: str, api_key: str, *, voice: str = _DEFAULT_VOICE, base_url: str = _BASE
    ) -> None:
        self.model = model
        self._key = api_key
        self._voice = voice or _DEFAULT_VOICE
        self._base = base_url.rstrip("/")

    async def synthesize(self, text: str, *, fmt: str = "mp3") -> GeneratedAudio:
        codec = fmt if fmt in _FORMAT else "mp3"
        async with httpx.AsyncClient(timeout=SPEECH_TIMEOUT_S) as http:
            r = await http.post(
                f"{self._base}/audio/speech",
                headers={"Authorization": f"Bearer {self._key}"},
                json={
                    "model": self.model, "voice": self._voice,
                    "input": text, "response_format": _FORMAT[codec],
                },
            )
            r.raise_for_status()
            return GeneratedAudio(r.content, _MIME[codec])
