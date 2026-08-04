"""Provider-agnostic media-generation interfaces.

These mirror `llm/`: one neutral interface per capability, with provider adapters
alongside, built by `build_*_client` (see __init__.py). The capabilities here are
the ones that are genuinely *separate services* from the chat model — image
generation, video generation, speech (TTS) and hearing (ASR) — each configured
independently, so the companion can pick (say) a local image model and a cloud
voice, or none at all. (Vision/image *understanding* is NOT here — it's a chat
capability and lives behind `llm/`.)
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass

# Bounded per-request timeouts: media work is slow, but never unbounded — a stalled
# call must not wedge the daemon's single consumer. Video is longest (async submit +
# poll); transcription/speech are quicker.
IMAGE_TIMEOUT_S = 120.0
SPEECH_TIMEOUT_S = 60.0
HEARING_TIMEOUT_S = 120.0
VIDEO_TIMEOUT_S = 300.0

# Shared adapter constants — part of the neutral interface's contract, single-sourced so
# sibling adapters can't drift. (Provider-specific endpoints like OpenAI's/Gemini's stay in
# their own adapters; only what's genuinely shared lives here.)
XAI_BASE_URL = "https://api.x.ai/v1"  # the endpoint every Grok media adapter hits
# OpenAI-style size → aspect ratio. Portrait is 3:4 (a natural portrait photo), NOT 9:16 —
# 9:16 is a tall phone shape that stretches a character shot.
IMAGE_ASPECT = {"1024x1024": "1:1", "1024x1536": "3:4", "1536x1024": "4:3"}
# Synthesized-audio container → mime type (IANA).
AUDIO_MIME = {"mp3": "audio/mpeg", "wav": "audio/wav", "ogg": "audio/ogg"}


@dataclass
class GeneratedImage:
    data: bytes
    mime: str = "image/png"


@dataclass
class GeneratedAudio:
    data: bytes
    mime: str = "audio/mpeg"


@dataclass
class GeneratedVideo:
    data: bytes
    mime: str = "video/mp4"


@dataclass
class Transcript:
    text: str
    language: str = ""


class ImageClient(ABC):
    """Text → image (optionally conditioned on a reference image, for Moko's own
    consistent character). Adapters translate to the provider's wire format; nothing
    above `media/` knows which provider is in use."""

    model: str
    # Whether this provider does real *reference-guided generation* — taking a reference
    # image as a character/style guide to compose a NEW picture. True for Gemini/OpenAI;
    # False for xAI, whose only image-input path is an *edit* that transforms the source
    # rather than referencing it (so for xAI we lean on the text character description).
    supports_reference: bool = False

    @abstractmethod
    async def generate(
        self,
        prompt: str,
        *,
        size: str = "1024x1024",
        reference: bytes | None = None,
        reference_mime: str = "image/png",
        scene: bytes | None = None,
    ) -> GeneratedImage:
        """Generate an image from `prompt`. When `reference` is given, the image is
        conditioned on it (an edit/variation) — how a consistent Moko self-portrait is
        produced from the locked character reference. `scene` is a SECOND conditioning
        image — the place the picture is set in (a room's canon view), so a selfie's
        background is the real recorded room, not a re-imagining. Adapters that can only
        take one input image ignore it (the text brief still grounds the scene)."""


class TTSClient(ABC):
    """Text → speech: Moko's own voice. Returns the synthesized audio bytes; the brain
    saves them as a sourced outbound artifact and sends them like any other file."""

    model: str

    @abstractmethod
    async def synthesize(self, text: str, *, fmt: str = "mp3") -> GeneratedAudio:
        """Speak `text`. `fmt` is the desired container ("mp3"|"wav"|"ogg"); adapters
        map it to the provider's encoding and report the actual mime on the result."""


class STTClient(ABC):
    """Speech → text: Moko's hearing. Transcribes an inbound voice note so the brain
    perceives what was said. The companion's own audio — free to process (no gate)."""

    model: str

    @abstractmethod
    async def transcribe(self, audio: bytes, *, mime: str = "audio/ogg") -> Transcript:
        """Transcribe `audio` to text. Returns an empty transcript (never raises for an
        unintelligible clip) so a failed listen degrades to 'a voice note arrived'."""


class VideoClient(ABC):
    """Text → video (optionally animating a reference image). Slow + async under the
    hood (submit then poll); adapters hide that and return the finished bytes."""

    model: str

    @abstractmethod
    async def generate(
        self,
        prompt: str,
        *,
        aspect_ratio: str = "16:9",
        reference: bytes | None = None,
        reference_mime: str = "image/png",
    ) -> GeneratedVideo:
        """Generate a short video from `prompt`. When `reference` is given, the video
        animates that image (image→video) instead of generating from text alone."""
