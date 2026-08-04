"""xAI "Grok Imagine" image adapter.

xAI's text→image *generation* speaks the OpenAI protocol (so we reuse the OpenAI
SDK via base_url), but with two twists the plain OpenAI adapter can't do: it has
no `size` param (it uses `aspect_ratio` + `resolution`, passed via `extra_body`),
and its reference/edit path is a JSON `/v1/images/edits` body (an `image_url` data
URI) — NOT OpenAI's multipart upload. So generation goes through the SDK and
editing through a raw httpx POST. Responses are URL-by-default; we request
`b64_json` and fall back to fetching a URL. Documented shape; needs a live key to
fully validate (tests fake the seam).
"""

from __future__ import annotations

import base64
from collections.abc import Awaitable, Callable

import httpx
from openai import AsyncOpenAI

from .base import IMAGE_ASPECT, IMAGE_TIMEOUT_S, XAI_BASE_URL, GeneratedImage, ImageClient

_BASE = XAI_BASE_URL
_ASPECT = IMAGE_ASPECT  # shared size→aspect map (xAI portrait options: 9:16, 3:4, 2:3)
_RESOLUTION = "1k"  # xAI's default tier (also accepts "2k"); follows Hermes' default


class XaiImageClient(ImageClient):
    """`api_key` for an explicit XAI_API_KEY, or `refresh` for the Grok *subscription*
    (OAuth) — an async hook returning a current bearer, refreshed before each call,
    exactly like the chat path. The OAuth token is the same `Bearer` against the same
    `api.x.ai/v1`, so one Grok login powers chat and images alike (Hermes' approach)."""

    # xAI's only image-input path is /images/edits, which *transforms* the source image
    # rather than using it as a character/style reference for a fresh composition — so we
    # don't treat it as reference-capable; selfies lean on the text character description.
    supports_reference = False

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
        self._client = AsyncOpenAI(api_key=self._key or "pending", base_url=_BASE,
                                   timeout=IMAGE_TIMEOUT_S)

    async def _bearer(self) -> str:
        """The current bearer — refreshed from OAuth when in subscription mode."""
        if self._refresh is not None:
            self._key = await self._refresh(False)
            self._client.api_key = self._key
        return self._key

    @staticmethod
    def _decode(datum: dict | object) -> GeneratedImage | None:
        get = datum.get if isinstance(datum, dict) else lambda k: getattr(datum, k, None)
        b64 = get("b64_json")
        if b64:
            return GeneratedImage(base64.b64decode(b64), "image/png")
        return None

    async def _fetch_url(self, url: str) -> GeneratedImage:
        # xAI returns ephemeral imgen.x.ai URLs that 404 within minutes — fetch now.
        async with httpx.AsyncClient(timeout=IMAGE_TIMEOUT_S) as http:
            r = await http.get(url)
            r.raise_for_status()
            return GeneratedImage(r.content, r.headers.get("content-type", "image/png"))

    async def generate(
        self,
        prompt: str,
        *,
        size: str = "1024x1024",
        reference: bytes | None = None,
        reference_mime: str = "image/png",
        scene: bytes | None = None,  # accepted for the interface; xAI has no reference path
    ) -> GeneratedImage:
        bearer = await self._bearer()
        if reference is not None:
            return await self._edit(prompt, reference, reference_mime, bearer, size)
        resp = await self._client.images.generate(
            model=self.model, prompt=prompt, n=1, response_format="b64_json",
            extra_body={"aspect_ratio": _ASPECT.get(size, "1:1"), "resolution": _RESOLUTION},
        )
        datum = resp.data[0]
        return self._decode(datum) or await self._fetch_url(getattr(datum, "url", "") or "")

    async def _edit(
        self, prompt: str, reference: bytes, reference_mime: str, bearer: str,
        size: str = "1024x1024",
    ) -> GeneratedImage:
        """Reference-conditioned generation via xAI's JSON edits endpoint (the OpenAI
        SDK's multipart edit won't fit this shape). We also pass the requested
        `aspect_ratio`/`resolution` so a referenced selfie still comes out portrait —
        xAI ignores fields it doesn't honour, so this is safe either way."""
        data_uri = f"data:{reference_mime};base64,{base64.b64encode(reference).decode('ascii')}"
        async with httpx.AsyncClient(timeout=IMAGE_TIMEOUT_S) as http:
            r = await http.post(
                f"{_BASE}/images/edits",
                headers={"Authorization": f"Bearer {bearer}"},
                json={
                    "model": self.model, "prompt": prompt,
                    "image": {"type": "image_url", "url": data_uri},
                    "response_format": "b64_json",
                    "aspect_ratio": _ASPECT.get(size, "1:1"), "resolution": _RESOLUTION,
                },
            )
            r.raise_for_status()
            datum = (r.json().get("data") or [{}])[0]
        decoded = self._decode(datum)
        if decoded:
            return decoded
        if datum.get("url"):
            return await self._fetch_url(datum["url"])
        raise RuntimeError("xAI returned no image data")
