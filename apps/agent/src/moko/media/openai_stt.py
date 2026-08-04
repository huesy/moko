"""OpenAI Whisper speech-to-text adapter.

`POST /v1/audio/transcriptions` as multipart (`file=`, `model=`) → JSON `{text}`.
Raw httpx so the seam is faked uniformly in tests. Default `whisper-1`. Never
raises for a bad clip — returns an empty transcript so a failed listen degrades
to "a voice note arrived".
"""

from __future__ import annotations

import httpx

from ..log import get_logger
from .base import HEARING_TIMEOUT_S, STTClient, Transcript

_BASE = "https://api.openai.com/v1"


class OpenAISTTClient(STTClient):
    def __init__(self, model: str, api_key: str, *, base_url: str = _BASE) -> None:
        self.model = model
        self._key = api_key
        self._base = base_url.rstrip("/")

    async def transcribe(self, audio: bytes, *, mime: str = "audio/ogg") -> Transcript:
        try:
            async with httpx.AsyncClient(timeout=HEARING_TIMEOUT_S) as http:
                r = await http.post(
                    f"{self._base}/audio/transcriptions",
                    headers={"Authorization": f"Bearer {self._key}"},
                    files={"file": ("audio", audio, mime)},
                    data={"model": self.model, "response_format": "json"},
                )
                r.raise_for_status()
                body = r.json()
        except (httpx.HTTPError, ValueError):
            get_logger("media").warning("OpenAI STT failed", exc_info=True)
            return Transcript("")
        return Transcript(str(body.get("text") or "").strip())
