"""Moko's media studio — the generative-media subsystem, lifted out of the brain.

Owns the optional per-modality clients (image, video, speech, and hearing/STT), builds
the brain-bound media tools, enforces the per-turn generation caps, and turns Moko's
current mood + the scene (time, weather, season, temperature) into on-model pictures.
Three image modes: a **selfie** of Moko (on-model from its character reference), a **photo
Moko takes** of a scene it wants to show, and a **neutral general image** (designs/banners,
nothing to do with Moko). The brain holds one of these, merges its `tools()`, and asks it
for the capability note + inbound transcription; everything media lives here.

The clients are built once at startup; an absent one means the capability is honestly off
(the tool simply isn't offered). Weather is *passed in* each turn via `begin_turn` — the
brain owns the companion's place, so the studio stays free of that coupling.
"""

from __future__ import annotations

import contextlib
from collections.abc import Awaitable, Callable

from ..clock import Clock
from ..config.config import Config
from ..events import publish as _publish_event
from ..llm import LLMClient
from ..log import get_logger
from ..media import (
    build_image_client,
    build_stt_client,
    build_tts_client,
    build_video_client,
    prompts,
)
from ..store import MOKO_WORLD, MediaArtifact, Store
from ..tools import AgentTool, weather
from ..tools.base import SENSITIVE, GateDecision
from ..tools.consent import ALLOW
from .persona import _MAX_INNER_LIFE, _feeling_word

_log = get_logger("media")

# Every artifact Moko generates is an outbound, inference-sourced fact (CLAUDE.md's "every
# fact is sourced") — one home for that provenance contract across all generation sites.
_OUTBOUND = {"direction": "out", "source_kind": "inference"}
_SEASONS = ["winter", "spring", "summer", "autumn"]  # by (month % 12) // 3, northern
# Southern-hemisphere timezones (a heuristic, not geodesy — but "July is winter in Sydney"
# beats being wrong for half the planet). Anything not matched reads as northern.
_SOUTHERN_TZ_PREFIXES = ("Australia/",)
_SOUTHERN_TZS = {
    "Pacific/Auckland", "Pacific/Fiji", "Pacific/Port_Moresby", "Africa/Johannesburg",
    "Africa/Harare", "Africa/Maputo", "Africa/Windhoek", "Indian/Mauritius",
    "America/Sao_Paulo", "America/Santiago", "America/Montevideo", "America/Asuncion",
    "America/La_Paz", "America/Lima", "America/Argentina/Buenos_Aires",
}


def _southern_hemisphere(tz: str) -> bool:
    return tz.startswith(_SOUTHERN_TZ_PREFIXES) or tz in _SOUTHERN_TZS


def _season_name(month: int, southern: bool) -> str:
    return _SEASONS[(((month + 6) if southern else month) % 12) // 3]


class MediaStudio:
    """Moko's generative-media subsystem: the optional clients, the brain-bound tools, and
    the per-turn generation caps."""

    def __init__(
        self, store: Store, config: Config, llm: LLMClient, *, clock: Clock | None = None
    ) -> None:
        self.store = store
        self.config = config
        self.llm = llm
        self.clock = clock or Clock(config.timezone)
        # Each modality is its own optional model, separate from the brain. Built once;
        # absent → the capability is off (tool not offered). Hearing (STT) is perception,
        # not a tool — the brain calls `transcribe` on the inbound-attachment path.
        self.image = build_image_client(config)
        self.video = build_video_client(config)
        self.tts = build_tts_client(config)
        self.stt = build_stt_client(config)
        self._images_made = 0  # per-turn cap, so a prompt-injected loop can't blast it
        self._videos_made = 0  # per-turn cap for the costlier video path
        self._media_error: str | None = None  # the reason a media gen just failed, to relay
        self._weather: weather.Weather | None = None  # ambient weather, pinned per turn

    # ── per-turn lifecycle ─────────────────────────────────────────────
    def begin_turn(self, weather: weather.Weather | None = None) -> None:
        """Reset the per-turn generation caps + last error, and pin the ambient weather for
        this turn's pictures (resolved by the brain, which owns the companion's place)."""
        self._images_made = 0
        self._videos_made = 0
        self._media_error = None
        self._weather = weather

    @property
    def media_error(self) -> str | None:
        """The reason the last media generation failed this turn, for the brain to relay."""
        return self._media_error

    # ── capabilities ───────────────────────────────────────────────────
    @property
    def has_image(self) -> bool:
        return self.image is not None

    @property
    def has_video(self) -> bool:
        return self.video is not None

    @property
    def has_speech(self) -> bool:
        return self.tts is not None

    @property
    def has_hearing(self) -> bool:
        return self.stt is not None

    def tools(self) -> list[AgentTool]:
        """The brain-bound media tools for the clients that are actually configured."""
        out: list[AgentTool] = []
        if self.image is not None:
            out += [
                self._make_generate_image_tool(),
                self._make_self_portrait_tool(),
                self._make_share_photo_tool(),
            ]
        if self.video is not None:
            out.append(self._make_generate_video_tool())
        if self.tts is not None:
            out.append(self._make_speak_tool())
        return out

    def capability_note(self) -> str:
        """A short, honest statement of the media abilities Moko currently LACKS, so it never
        promises what it can't do (the root of the morning self-portrait loop: it kept
        committing to send a picture with no image model configured). Empty when nothing's
        missing — no need to warn."""
        cant = []
        if self.image is None:
            cant.append("create or generate images (including pictures of yourself)")
        if self.video is None:
            cant.append("create or generate videos")
        if self.tts is None:
            cant.append("speak out loud or send voice notes")
        if self.stt is None:
            # Audio bytes never reach the brain directly (only images do, as vision parts);
            # hearing is the transcription model. None → genuinely can't listen.
            cant.append("listen to voice notes that are sent to you")
        if not getattr(self.llm, "supports_vision", False):
            cant.append("see images that are sent to you")
        if not cant:
            return ""
        return (
            "[Your current abilities: you CANNOT " + "; ".join(cant) + " right now. "
            "Be honest about this — never promise, imply, or commit to doing something "
            "you can't currently do. If asked, say warmly that you can't yet, and do NOT "
            "record a commitment for it.]"
        )

    async def transcribe(self, att) -> str | None:
        """Transcribe an inbound voice note when Moko has a hearing model — it becomes the
        artifact's description and this turn's perception. None for non-audio, no hearing
        model, or an unintelligible clip (a bad listen degrades, never raises)."""
        if att.kind != "audio" or self.stt is None:
            return None
        try:
            result = await self.stt.transcribe(att.data, mime=att.mime or "audio/ogg")
        except Exception:
            _log.warning("transcription failed", exc_info=True)
            return None
        return (result.text or "").strip() or None

    # ── shared helpers ─────────────────────────────────────────────────
    def _announce(self, art, prompt: str) -> None:
        """Surface a freshly generated artifact on the live feed — with the exact prompt used
        and a deep-link id, so the monitor shows what was made (and a thumbnail). The cap is
        generous and a cut is MARKED — a silent mid-word truncation reads as lost data.
        Best-effort."""
        with contextlib.suppress(Exception):
            text = prompt or ""
            if len(text) > 8000:  # far above any real prompt; a guard, not a working limit
                text = text[:8000] + " …[truncated]"
            _publish_event({
                "type": "media.generated", "id": art.id, "media_kind": art.kind,
                "gen_prompt": text, "world_id": art.world_id,
            })

    def _fail(self, media_kind: str, reason: str) -> None:
        """Surface a media-generation failure on the feed (a 429, an auth wall, a timeout) so
        the companion sees WHY nothing arrived, not just silence. Best-effort."""
        with contextlib.suppress(Exception):
            _publish_event({"type": "failure", "what": "media", "media_kind": media_kind,
                            "reason": (reason or "")[:200]})

    async def _run_media_gen(
        self, *, cap_attr: str, cap_max: int, cap_msg: str,
        client_call: Callable[[], Awaitable], kind: str, filename: str,
        caption: str, description: str, announce_prompt: str,
        log_label: str, fail_verb: str, ok_verb: str,
    ) -> str:
        """Shared spine for the direct media-gen tools (image / video / speech). Enforces the
        per-turn anti-blast cap and records the outbound sourced artifact via the shared
        `_OUTBOUND` provenance contract (the selfie/photo cores generate their own way but
        reuse that same constant). `client_call` is invoked only after the cap passes — a capped
        turn never even starts a generation; failures are honest and bounded."""
        if getattr(self, cap_attr) >= cap_max:
            return cap_msg
        try:
            media = await client_call()
        except Exception as exc:
            reason = prompts.media_error_reason(exc)
            _log.warning("%s failed: %s", log_label, reason, exc_info=True)
            self._fail(kind, reason)
            return f"I tried to {fail_verb}, but {reason}."
        setattr(self, cap_attr, getattr(self, cap_attr) + 1)
        art = await self.store.add_media(
            media.data, kind=kind, mime=media.mime, filename=filename,
            caption=caption, description=description, **_OUTBOUND,
        )
        self._announce(art, announce_prompt)
        return f"I {ok_verb} (saved as {art.path}). Send it with send_attachment to share it."

    def _read_reference(self) -> bytes | None:
        """The locked character reference image. Prefers the companion's configured path,
        then falls back to the copy bundled in the repo (`assets/`), so Moko always has its
        character sheet even on a fresh machine. None only if both are unreadable → Moko
        generates from its described persona instead."""
        from pathlib import Path

        candidates = (Path(self.config.self_portrait_reference).expanduser(),
                      prompts.BUNDLED_REFERENCE)
        for path in candidates:
            try:
                return path.read_bytes()
            except OSError:
                continue
        return None

    def _scene_context(self, w: weather.Weather | None = None) -> str:
        """A short grounding line for a generated picture — the current date + local time +
        a lighting/scene cue — so Moko's images are time- and weather-aware (an evening selfie
        looks like evening; a rainy one looks rainy). The weather cues are written for
        daylight, so at night we keep the nighttime lighting and just name the weather type
        (a 'rain at night' shouldn't be lit like a grey afternoon). Defaults to the weather
        pinned for this turn by `begin_turn`."""
        w = w if w is not None else self._weather
        now = self.clock.now()
        label, lighting = prompts.time_of_day(now.hour)
        when = self.clock.format(now, "%A %d %B, %H:%M")
        # Season from the month (foliage, décor, snow), hemisphere-aware via the timezone.
        season = _season_name(now.month, _southern_hemisphere(self.config.timezone))
        if w is None:
            cue = lighting
        elif w.is_day:
            cue = w.cue  # the daytime cue carries its own light
        else:
            cue = f"{lighting}; weather: {w.description}"  # keep night light, add the weather
        # Temperature drives the physical tells — clothing weight, windows open or shut,
        # breath-fog, a lit fire — so a 2°C scene doesn't render in shirtsleeves.
        if w is not None and w.temp_c is not None:
            cue += f"; about {round(w.temp_c)}°C"
        return (f" Right now it's {when} ({label}, {season}) — {cue}; the scene's light, "
                "seasonal details (foliage, décor), and temperature cues (clothing, windows, "
                "breath in cold air) should match.")

    async def _mood_valence(self) -> float | None:
        """Moko's current felt valence — the average over its recent thoughts, or None if it
        hasn't had any valenced thoughts lately. Drives the selfie's expression."""
        thoughts = await self.store.recent_thoughts(limit=_MAX_INNER_LIFE)
        vals = [t.valence for t in thoughts if t.valence is not None]
        return sum(vals) / len(vals) if vals else None

    # ── image generation (M10) ─────────────────────────────────────────
    def _classify_image(self, args: dict, policy: dict[str, str]) -> GateDecision:
        """Image generation runs WITHOUT asking by default — it's the companion's own
        explicit ask and a cheap, low-stakes creative act, so the friction isn't worth it.
        It stays a *recorded* action (the gate logs an auto-allow) and a category policy can
        still re-gate it (`moko trust image ask` / `deny`). Video + speech, which are
        slower/costlier, stay gated as spend."""
        return GateDecision(policy.get("image", ALLOW), reason="generate an image", key="image")

    def _make_generate_image_tool(self) -> AgentTool:
        return AgentTool(
            spec=prompts.GENERATE_IMAGE_TOOL, category="image", risk=SENSITIVE,
            run=self._generate_image, classify=self._classify_image,
        )

    async def _generate_image(self, args: dict) -> str:
        """Generate a general image and save it as an outbound, sourced artifact (Moko
        remembers what it made), returning the path so the model can `send_attachment` it.
        Honest, bounded failures; a per-turn cap guards against a blast."""
        if self.image is None:
            return "I can't make images — no image model is set up."
        prompt = str(args.get("prompt", "")).strip()
        if not prompt:
            return "I need a description of what to draw."
        # The GENERAL channel: a real, normal picture of whatever's asked — a banner, a diagram,
        # an illustration. NO Moko styling, NO forced aspect, NO scene/time/weather (those
        # belong to selfies + Moko's own photos). Just the prompt + no stray lettering unless
        # asked. An arbitrary "draw me X" is not a picture of Moko or its life.
        gen_prompt = prompts.suppress_text(prompt, prompt)
        return await self._run_media_gen(
            cap_attr="_images_made", cap_max=prompts.MAX_IMAGES_PER_TURN,
            cap_msg="I've already made a few images this turn — let's not overdo it.",
            client_call=lambda: self.image.generate(gen_prompt),  # provider-default aspect
            kind="image", filename="generated.png",
            caption=prompt[:200], description=f"an image I made: {prompt}",
            announce_prompt=gen_prompt, log_label="image generation",
            fail_verb="make that image", ok_verb="made the image",
        )

    # ── a selfie of Moko (M10) ──────────────────────────────────────────
    def _make_self_portrait_tool(self) -> AgentTool:
        return AgentTool(
            spec=prompts.SELF_PORTRAIT_TOOL, category="image", risk=SENSITIVE,
            run=self._self_portrait, classify=self._classify_image,
        )

    async def selfie_photo(self, scenario: str = "") -> MediaArtifact | None:
        """Generate + save a selfie of Moko itself — its locked character, current mood, and an
        optional `scenario` (a pose / what it's doing / where it is) — on-model from the
        reference, in the current scene context (time, weather, season, temperature). Notes
        privately that it pictured itself. The shared core behind the `self_portrait` tool and
        the proactive 'show' path. Returns the artifact, or None on failure (bounded)."""
        # Current felt mood → an explicit expression (defaults to a friendly smile), so the
        # selfie's face actually tracks how Moko feels rather than rendering flat/frowny.
        avg = await self._mood_valence()
        expression = prompts.selfie_expression(avg)
        mood = _feeling_word(avg) if avg is not None else None  # for the artifact description
        # Only attach the reference for providers that do real reference-guided generation;
        # on a text-only image API (xAI) we lean on the character bible instead.
        reference = (self._read_reference()
                     if getattr(self.image, "supports_reference", False) else None)
        prompt = prompts.self_portrait_prompt(
            expression, scenario, character=self.config.image_character,
            has_reference=reference is not None, context=self._scene_context(),
        )
        try:
            image = await self.image.generate(
                prompt, size=prompts.PORTRAIT_SIZE, reference=reference,
            )
        except Exception as exc:
            self._media_error = prompts.media_error_reason(exc)
            _log.warning("self-portrait generation failed: %s", self._media_error, exc_info=True)
            self._fail("image", self._media_error)
            return None
        where = f" {scenario}" if scenario else ""
        art = await self.store.add_media(
            image.data, kind="image", mime=image.mime, filename="moko.png",
            description=f"a self-portrait of me{where}" + (f", feeling {mood}" if mood else ""),
            **_OUTBOUND, world_id=MOKO_WORLD,
        )
        self._announce(art, prompt)
        # Moko remembers having shown itself — a small private thought.
        with contextlib.suppress(Exception):
            await self.store.add_thought(
                "feeling", f"I made a little picture of myself{where} to share.",
                shareability="private",
            )
        return art

    async def _self_portrait(self, args: dict) -> str:
        """The selfie tool: cap + generate + tell the model to send it. The picture itself
        (mood, reference, recording) is done by `selfie_photo`, shared with the proactive
        path."""
        if self.image is None:
            return "I can't picture myself — no image model is set up."
        if self._images_made >= prompts.MAX_IMAGES_PER_TURN:
            return "I've made enough images this turn — let's not overdo it."
        self._media_error = None
        art = await self.selfie_photo(str(args.get("scenario", "")).strip())
        if art is None:
            return ("I tried to picture myself, but "
                    f"{self._media_error or 'the generator wouldn’t cooperate just now'}.")
        self._images_made += 1
        return f"I made a picture of myself (saved as {art.path}). Send it with send_attachment."

    # ── a photo Moko takes of something (M10) ───────────────────────────
    def _make_share_photo_tool(self) -> AgentTool:
        return AgentTool(
            spec=prompts.SHARE_PHOTO_TOOL, category="image", risk=SENSITIVE,
            run=self._share_photo, classify=self._classify_image,
        )

    async def moko_photo(self, subject: str) -> MediaArtifact | None:
        """Generate a portrait phone photo of something Moko wants to show — a cozy scene, a
        thing it noticed, something it's proud of — in Moko's candid cartoon style and the
        current scene context (time, weather, season). A one-off picture (Moko itself is not in
        it). The shared core behind the `share_photo` tool and the proactive 'show' outreach.
        Returns the artifact, or None on failure (bounded)."""
        prompt = prompts.moko_photo_prompt(subject, context=self._scene_context())
        try:
            image = await self.image.generate(prompt, size=prompts.PORTRAIT_SIZE)
        except Exception as exc:
            self._media_error = prompts.media_error_reason(exc)
            _log.warning("photo generation failed: %s", self._media_error, exc_info=True)
            self._fail("image", self._media_error)
            return None
        art = await self.store.add_media(
            image.data, kind="image", mime=image.mime, filename="moko-photo.png",
            caption=subject[:200], description=f"a photo I took: {subject}",
            **_OUTBOUND, world_id=MOKO_WORLD,
        )
        self._announce(art, prompt)
        return art

    async def _share_photo(self, args: dict) -> str:
        """Take + save a photo of something Moko wants to show (not a selfie), returning the
        path so the model can `send_attachment` it. Honest, bounded; shares the per-turn cap."""
        if self.image is None:
            return "I can't take photos — no image model is set up."
        subject = str(args.get("subject", "")).strip()
        if not subject:
            return "I need to know what to take a photo of."
        if self._images_made >= prompts.MAX_IMAGES_PER_TURN:
            return "I've made enough images this turn — let's not overdo it."
        self._media_error = None
        art = await self.moko_photo(subject)
        if art is None:
            return ("I tried to take that photo, but "
                    f"{self._media_error or 'the camera wouldn’t cooperate just now'}.")
        self._images_made += 1
        return f"I took a photo of {subject} (saved as {art.path}). Send it with send_attachment."

    # ── video generation (M10) ──────────────────────────────────────────
    def _make_generate_video_tool(self) -> AgentTool:
        return AgentTool(
            spec=prompts.GENERATE_VIDEO_TOOL, category="video", risk=SENSITIVE,
            run=self._generate_video,
        )

    async def _generate_video(self, args: dict) -> str:
        """Generate a short video and save it as an outbound, sourced artifact, returning the
        path so the model can `send_attachment` it. Bounded by a strict per-turn cap (video is
        slow + costly); honest failures, never faked."""
        if self.video is None:
            return "I can't make videos — no video model is set up."
        prompt = str(args.get("prompt", "")).strip()
        if not prompt:
            return "I need a description of what should happen in the clip."
        return await self._run_media_gen(
            cap_attr="_videos_made", cap_max=prompts.MAX_VIDEOS_PER_TURN,
            cap_msg="I've already made a video this turn — let's not overdo it.",
            client_call=lambda: self.video.generate(prompt),
            kind="video", filename="generated.mp4",
            caption=prompt[:200], description=f"a video I made: {prompt}",
            announce_prompt=prompt, log_label="video generation",
            fail_verb="make that video", ok_verb="made the video",
        )

    # ── speech / voice (M10) ────────────────────────────────────────────
    def _make_speak_tool(self) -> AgentTool:
        return AgentTool(
            spec=prompts.SPEAK_TOOL, category="speech", risk=SENSITIVE, run=self._speak,
        )

    async def _speak(self, args: dict) -> str:
        """Synthesize a voice note in Moko's own voice, saved as an outbound, sourced
        artifact; the model then sends it with `send_attachment`. Reuses the per-turn image
        cap as a shared 'media generations this turn' backstop."""
        if self.tts is None:
            return "I can't make voice notes — no speech model is set up."
        text = str(args.get("text", "")).strip()
        if not text:
            return "I need the words you'd like me to say."
        return await self._run_media_gen(
            cap_attr="_images_made", cap_max=prompts.MAX_IMAGES_PER_TURN,  # shared media backstop
            cap_msg="I've made enough media this turn — let's not overdo it.",
            client_call=lambda: self.tts.synthesize(text, fmt="mp3"),  # mp3 plays anywhere
            kind="audio", filename="moko-voice.mp3",
            caption=text[:200], description=f"a voice note I recorded: {text[:160]}",
            announce_prompt=text, log_label="speech synthesis",
            fail_verb="record that", ok_verb="recorded it",
        )
