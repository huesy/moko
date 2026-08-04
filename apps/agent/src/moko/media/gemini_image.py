"""Google Gemini image-generation adapter (httpx REST — no vendor SDK).

Uses the native `:generateContent` endpoint (the recommended path for image models
like `gemini-2.5-flash-image` / "Nano Banana"): a text part → an image part, or an
input image part + text → an edited image (so a self-portrait can stay on-model
from the reference). Auth is the `x-goog-api-key` header. The image comes back as
base64 inside a heterogeneous `parts` array — we pick the part carrying inline
data. Best-effort, documented shape; needs a live key to fully validate (tests
mock the transport).
"""

from __future__ import annotations

import base64

import httpx

from .base import IMAGE_ASPECT, IMAGE_TIMEOUT_S, GeneratedImage, ImageClient

_BASE = "https://generativelanguage.googleapis.com/v1beta/models"
# Gemini otherwise follows the *reference* image's aspect (a landscape character sheet → a
# landscape selfie), so the size→aspect must be sent explicitly (shared map).
_ASPECT = IMAGE_ASPECT


class GeminiImageClient(ImageClient):
    supports_reference = True  # generateContent uses the reference as true generation context

    def __init__(self, model: str, api_key: str) -> None:
        self.model = model
        self._key = api_key

    async def generate(
        self,
        prompt: str,
        *,
        size: str = "1024x1024",  # accepted for the interface; Gemini sizes by prompt
        reference: bytes | None = None,
        reference_mime: str = "image/png",
        scene: bytes | None = None,
    ) -> GeneratedImage:
        parts: list[dict] = []
        if reference is not None:  # an input image conditions/edits the result
            parts.append({"inlineData": {
                "mimeType": reference_mime,
                "data": base64.b64encode(reference).decode("ascii"),
            }})
        if scene is not None:  # a second conditioning image: the room the picture is set in
            parts.append({"inlineData": {
                "mimeType": "image/png",
                "data": base64.b64encode(scene).decode("ascii"),
            }})
        parts.append({"text": prompt})
        body = {
            "contents": [{"parts": parts}],
            "generationConfig": {
                "responseModalities": ["TEXT", "IMAGE"],
                # Force the output aspect — otherwise Gemini inherits the reference's shape.
                "imageConfig": {"aspectRatio": _ASPECT.get(size, "1:1")},
            },
        }
        url = f"{_BASE}/{self.model}:generateContent"
        async with httpx.AsyncClient(timeout=IMAGE_TIMEOUT_S) as http:
            resp = await http.post(
                url,
                headers={"x-goog-api-key": self._key, "Content-Type": "application/json"},
                json=body,
            )
            resp.raise_for_status()
            data = resp.json()
        for candidate in data.get("candidates", []):
            for part in (candidate.get("content") or {}).get("parts", []):
                # REST returns camelCase; tolerate snake_case just in case.
                inline = part.get("inlineData") or part.get("inline_data")
                if inline and inline.get("data"):
                    mime = inline.get("mimeType") or inline.get("mime_type") or "image/png"
                    return GeneratedImage(base64.b64decode(inline["data"]), mime)
        raise RuntimeError("Gemini returned no image data")
