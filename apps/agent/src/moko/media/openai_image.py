"""OpenAI-protocol image adapter — serves OpenAI (gpt-image-1 / DALL·E) AND, via
``base_url``, xAI's **Grok Imagine** (its image API is OpenAI-compatible).

Reuses the OpenAI SDK already vendored for chat. `generate` returns raw bytes; it
handles both response shapes the protocol produces — inline base64 (`b64_json`,
gpt-image-1 / Grok) and a hosted `url` (DALL·E default), fetched over httpx. A
`reference` routes through the edits endpoint so a self-portrait stays on-model
(providers without an edit endpoint surface a clean error to the caller). The wire
shape follows the documented SDK; it needs a live key to fully validate (tests
fake the seam).
"""

from __future__ import annotations

import base64
import io

import httpx
from openai import AsyncOpenAI

from .base import IMAGE_TIMEOUT_S, GeneratedImage, ImageClient


class OpenAIImageClient(ImageClient):
    supports_reference = True  # gpt-image-1's images.edit does real reference-guided editing

    def __init__(self, model: str, api_key: str, *, base_url: str | None = None) -> None:
        self.model = model
        self._client = AsyncOpenAI(api_key=api_key, base_url=base_url, timeout=IMAGE_TIMEOUT_S)

    async def generate(
        self,
        prompt: str,
        *,
        size: str = "1024x1024",
        reference: bytes | None = None,
        reference_mime: str = "image/png",
        scene: bytes | None = None,
    ) -> GeneratedImage:
        inputs = []
        for i, data in enumerate(b for b in (reference, scene) if b is not None):
            buf = io.BytesIO(data)
            buf.name = f"reference-{i}.png"  # the SDK keys the upload off a filename
            inputs.append(buf)
        if inputs:
            # gpt-image models accept a LIST of input images (character + scene); older
            # DALL·E-style edits only take one, so a single input stays a bare object.
            resp = await self._client.images.edit(
                model=self.model, image=inputs if len(inputs) > 1 else inputs[0],
                prompt=prompt, size=size,
            )
        else:
            resp = await self._client.images.generate(
                model=self.model, prompt=prompt, size=size,
            )
        datum = resp.data[0]
        b64 = getattr(datum, "b64_json", None)
        if b64:
            return GeneratedImage(base64.b64decode(b64), "image/png")
        url = getattr(datum, "url", None)
        if url:  # DALL·E (and some compatible providers) return a hosted URL instead
            async with httpx.AsyncClient(timeout=IMAGE_TIMEOUT_S) as http:
                fetched = await http.get(url)
                fetched.raise_for_status()
                return GeneratedImage(fetched.content,
                                      fetched.headers.get("content-type", "image/png"))
        raise RuntimeError("image provider returned no image data")
