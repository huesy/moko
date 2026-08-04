"""xAI "Grok Imagine" video adapter.

xAI's video API is asynchronous: `POST /v1/videos/generations` returns a
`request_id`, then `GET /v1/videos/{id}` is polled until `status == "done"`,
yielding an ephemeral CDN URL we fetch immediately. Text→video uses
`grok-imagine-video`; passing a reference image switches to image→video
(`grok-imagine-video-1.5-preview`) — the model animates that frame. Auth mirrors
the other xAI adapters (XAI_API_KEY or the Grok subscription via `refresh`).
Documented shape (per Hermes' proven adapter); the seam is faked in tests.
"""

from __future__ import annotations

import asyncio
import base64
from collections.abc import Awaitable, Callable

import httpx

from .base import VIDEO_TIMEOUT_S, XAI_BASE_URL, GeneratedVideo, VideoClient

_BASE = XAI_BASE_URL
_TEXT_MODEL = "grok-imagine-video"
_IMAGE_MODEL = "grok-imagine-video-1.5-preview"  # image→video backend
_VALID_ASPECT = {"1:1", "16:9", "9:16", "4:3", "3:4", "3:2", "2:3"}
_DEFAULT_ASPECT = "16:9"
_RESOLUTION = "720p"
_DURATION = 8  # seconds; xAI accepts 1–15
_POLL_INTERVAL_S = 5
_DONE = "done"
_TERMINAL_FAIL = {"failed", "error", "expired", "cancelled"}


class XaiVideoClient(VideoClient):
    """`api_key` for an explicit XAI_API_KEY, or `refresh` for the Grok subscription
    (OAuth). Generation is text→video; a reference image animates it (image→video)."""

    def __init__(
        self,
        model: str,
        api_key: str | None = None,
        *,
        refresh: Callable[[bool], Awaitable[str]] | None = None,
    ) -> None:
        self.model = model
        self._key = api_key or ""
        self._refresh = refresh

    async def _bearer(self) -> str:
        if self._refresh is not None:
            self._key = await self._refresh(False)
        return self._key

    def _model_for(self, has_reference: bool) -> str:
        """Route to the right backend for the modality (per Hermes): image→video always
        uses the preview model; text→video can't use the image-only model. A configured
        text model still routes image→video correctly, so one setting serves both."""
        configured = self.model
        if has_reference:
            return configured if configured and configured != _TEXT_MODEL else _IMAGE_MODEL
        return _TEXT_MODEL if configured in ("", _IMAGE_MODEL) else configured

    def _headers(self, bearer: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {bearer}", "Content-Type": "application/json"}

    async def generate(
        self,
        prompt: str,
        *,
        aspect_ratio: str = _DEFAULT_ASPECT,
        reference: bytes | None = None,
        reference_mime: str = "image/png",
    ) -> GeneratedVideo:
        bearer = await self._bearer()
        aspect = aspect_ratio if aspect_ratio in _VALID_ASPECT else _DEFAULT_ASPECT
        model = self._model_for(reference is not None)
        payload: dict = {
            "model": model, "prompt": prompt,
            "duration": _DURATION, "aspect_ratio": aspect, "resolution": _RESOLUTION,
        }
        if reference is not None:
            uri = f"data:{reference_mime};base64,{base64.b64encode(reference).decode('ascii')}"
            payload["image"] = {"url": uri}
        async with httpx.AsyncClient(timeout=VIDEO_TIMEOUT_S) as http:
            sub = await http.post(
                f"{_BASE}/videos/generations", headers=self._headers(bearer), json=payload,
            )
            sub.raise_for_status()
            request_id = sub.json().get("request_id")
            if not request_id:
                raise RuntimeError("xAI video response had no request_id")
            url = await self._poll(http, request_id, bearer)
            return await self._fetch(http, url)

    async def _poll(self, http: httpx.AsyncClient, request_id: str, bearer: str) -> str:
        """Poll until the job finishes; return the video URL (ephemeral — fetch now)."""
        elapsed = 0.0
        while elapsed < VIDEO_TIMEOUT_S:
            r = await http.get(f"{_BASE}/videos/{request_id}", headers=self._headers(bearer))
            r.raise_for_status()
            body = r.json()
            status = str(body.get("status") or "").lower()
            if status == _DONE:
                url = (body.get("video") or {}).get("url")
                if not url:
                    raise RuntimeError("xAI video finished without a URL")
                return url
            if status in _TERMINAL_FAIL:
                msg = (body.get("error") or {}).get("message") or status
                raise RuntimeError(f"xAI video generation {status}: {msg}")
            await asyncio.sleep(_POLL_INTERVAL_S)
            elapsed += _POLL_INTERVAL_S
        raise TimeoutError("xAI video generation timed out")

    async def _fetch(self, http: httpx.AsyncClient, url: str) -> GeneratedVideo:
        r = await http.get(url)
        r.raise_for_status()
        return GeneratedVideo(r.content, r.headers.get("content-type", "video/mp4"))
