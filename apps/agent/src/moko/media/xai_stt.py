"""xAI Grok speech-to-text adapter.

xAI exposes `POST /v1/stt` as multipart/form-data (`file=`, plus `language` /
`format` fields) → JSON `{text, language, duration}`. Auth mirrors the other xAI
adapters: an explicit `XAI_API_KEY`, or the Grok *subscription* (OAuth) via a
`refresh` hook. Documented shape (per Hermes' proven adapter); the seam is faked
in tests. Transcription never raises for a bad clip — it returns an empty
transcript so a failed listen degrades to "a voice note arrived".
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

import httpx

from ..log import get_logger
from .base import HEARING_TIMEOUT_S, XAI_BASE_URL, STTClient, Transcript

_BASE = XAI_BASE_URL
_DEFAULT_LANGUAGE = "en"


class XaiSTTClient(STTClient):
    """`api_key` for an explicit XAI_API_KEY, or `refresh` for the Grok subscription
    (OAuth) — refreshed before each call."""

    def __init__(
        self,
        model: str,
        api_key: str | None = None,
        *,
        language: str = _DEFAULT_LANGUAGE,
        refresh: Callable[[bool], Awaitable[str]] | None = None,
    ) -> None:
        self.model = model
        self._key = api_key or ""
        self._language = language or _DEFAULT_LANGUAGE
        self._refresh = refresh

    async def _bearer(self) -> str:
        if self._refresh is not None:
            self._key = await self._refresh(False)
        return self._key

    async def transcribe(self, audio: bytes, *, mime: str = "audio/ogg") -> Transcript:
        bearer = await self._bearer()
        try:
            async with httpx.AsyncClient(timeout=HEARING_TIMEOUT_S) as http:
                r = await http.post(
                    f"{_BASE}/stt",
                    headers={"Authorization": f"Bearer {bearer}"},
                    files={"file": ("audio", audio, mime)},
                    data={"language": self._language, "format": "true"},
                )
                r.raise_for_status()
                body = r.json()
        except (httpx.HTTPError, ValueError):
            get_logger("media").warning("xAI STT failed", exc_info=True)
            return Transcript("")
        return Transcript(str(body.get("text") or "").strip(), str(body.get("language") or ""))
