"""Image/media prompt building — the pure, stateless layer.

Style constants, the three image-prompt builders (selfie / Moko's photo / general), the
text-suppression and time-of-day helpers, the media-failure reason translator, and the
brain-bound media Tool specs. No I/O and no Brain state, so it's easy to test and is used
by the media studio (which calls the clients).
"""

from __future__ import annotations

import re
from pathlib import Path

from ..llm import Tool

# Bounded blast backstops even when the companion has trusted media generation.
MAX_IMAGES_PER_TURN = 3
MAX_VIDEOS_PER_TURN = 1  # video is slow + costly

PORTRAIT_SIZE = "1024x1536"  # portrait orientation — "like it came from a phone"

# The character sheet shipped in the repo (assets/), the fallback when the companion hasn't
# placed their own at config.self_portrait_reference. This file is src/moko/media/prompts.py,
# so parents[1] is src/moko/.
BUNDLED_REFERENCE = Path(__file__).resolve().parents[1] / "assets" / "moko-character-reference.png"

# Moko's world is ILLUSTRATED, not photographic. Calling the output a "photo" pushes the
# model to photorealism (the bug — semi-realistic results that ignore the cartoon
# reference), so every prompt frames the *composition* casually (a selfie, a quick shot to
# share) while pinning the *render* to the cartoon style. Split in two: the POSITIVE style
# description, and the anti-photorealism GUARD. With a reference image the positive style is
# already carried by the reference (line + shading), so restating "flat cartoon shading"
# would *fight* "match the reference's shading" — then we append only the guard.
_ART_STYLE_POSITIVE = (
    " Drawn in Moko's signature art style: a soft, rounded, hand-drawn cartoon illustration, "
    "cute and warm, with flat cartoon shading."
)
_NOT_PHOTO = (
    " A cartoon illustration, NOT photorealistic — no realistic textures, no real skin or "
    "fur detail, no photographic camera lighting."
)

# A hard anatomy constraint, appended to any picture *of Moko*. Image models love to add a
# third arm to hold the selfie camera (esp. with a "hands on the keyboard" scenario), or to
# fuse/deform limbs — so we pin the count explicitly.
_ANATOMY = (
    " Moko has exactly TWO short arms and TWO stubby legs — never a third arm, extra, "
    "missing, fused, or deformed limbs, hands, or fingers."
)

# Selfie composition (no appearance words baked in — the character description rides in
# separately). The *resulting selfie image* as the front camera captures it — a close-up of
# Moko — NOT a third-person scene of Moko holding a phone.
_SELFIE_FRAMING = (
    "A selfie of Moko, portrait orientation (taller than wide) — a close-up of Moko's face "
    "and upper body filling the frame, seen from arm's length straight on (a typical selfie "
    "angle). CRUCIAL: this image IS the finished photo exactly as the front camera captured "
    "it. The camera/phone is the unseen viewpoint and must NEVER appear — do NOT draw any "
    "camera, phone, or device anywhere in the image, and do NOT draw any arm reaching out to "
    "hold one. One of Moko's paws is the (unseen, off-frame) hand holding the camera, so no "
    "device shows; Moko's OTHER paw may be empty OR holding something that fits the moment "
    "(a mug, a flower, whatever the scenario suggests) — but NEVER a camera or phone. This is "
    "the resulting picture itself, NOT a third-person scene of Moko holding or using a phone."
)

# Appended only when a reference image is present. The reference is a *character model sheet*
# (full-body front/side/back views), NOT a base image to edit — so take only the character +
# its cartoon style and compose a new picture, not a copy of the sheet's layout.
_REFERENCE_STYLE = (
    " The reference image is a character model sheet of Moko (full-body front, side, and "
    "back views). Match it EXACTLY for Moko's appearance AND its cartoon art style — "
    "character design, proportions, colours, line and shading — so Moko stays perfectly "
    "on-model. Do NOT copy the sheet's layout or show multiple views; compose a single, "
    "brand-new picture in that same cartoon style."
)

# A picture of something in Moko's *world* (not a selfie). No character reference attached
# (it's a *character* sheet; sending it risks inserting Moko); consistency comes from style.
_WORLD_STYLE = " A glimpse of Moko's world — cozy and storybook."

# Words meaning the companion actually wants lettering in the image (so we don't strip a
# requested caption). Otherwise text is suppressed — AI images love to scrawl garbled words.
_TEXT_REQUEST_WORDS = {
    "text", "word", "words", "says", "saying", "caption", "captioned", "sign", "write",
    "written", "writing", "label", "labelled", "labeled", "title", "titled", "quote",
    "letter", "letters", "message", "banner", "subtitle", "meme",
}
_NO_TEXT = (
    " Do not put any text, words, letters, captions, watermarks, logos, or signatures "
    "anywhere in the image."
)


def wants_text(*phrases: str) -> bool:
    """True if any phrase explicitly asks for lettering — then we leave text in."""
    words = set(re.findall(r"[a-z]+", " ".join(phrases).lower()))
    return bool(words & _TEXT_REQUEST_WORDS)


def suppress_text(prompt: str, *request_phrases: str) -> str:
    """Append the no-text instruction unless the companion expressly asked for lettering."""
    return prompt if wants_text(*request_phrases) else prompt + _NO_TEXT


def time_of_day(hour: int) -> tuple[str, str]:
    """A part-of-day label + lighting cue for the given local hour, so a generated picture's
    light matches the real moment (a late-night selfie looks like cozy night, a morning one
    bright). Keeps Moko's images time-aware, like the rest of it."""
    if 5 <= hour < 8:
        return "early morning", "soft, cool early-morning light"
    if 8 <= hour < 11:
        return "morning", "bright, fresh morning light"
    if 11 <= hour < 15:
        return "midday", "bright, clear daytime light"
    if 15 <= hour < 18:
        return "afternoon", "warm afternoon light"
    if 18 <= hour < 21:
        return "evening", "warm, golden early-evening light"
    return "night", "nighttime — cosy, warm indoor lamplight, dark outside the window"


def selfie_expression(valence: float | None) -> str:
    """A facial expression for a selfie, from Moko's current felt valence. Defaults to a
    warm, friendly smile — a selfie reads happy unless Moko is genuinely low — and only
    turns down when its mood is real-negative. This stops a neutral/unknown mood from
    rendering a flat or frowny face, and makes the expression track how Moko feels now."""
    if valence is not None and valence <= -0.5:
        return "a little sad — a soft, gently downturned mouth"
    if valence is not None and valence <= -0.2:
        return "quiet and subdued, with a small, faint smile"
    if valence is not None and valence >= 0.5:
        return "delighted — a big, warm, happy smile"
    return "happy and content, with a warm, friendly smile"  # neutral / mild / unknown


def self_portrait_prompt(
    expression: str, scenario: str, *, character: str, has_reference: bool, context: str = "",
) -> str:
    """Build a selfie prompt: the selfie composition + how Moko looks (the reference model
    sheet when a reference-capable provider is in use, else the text `character` bible) +
    Moko's current felt **expression** + what it's doing (`scenario`, a free-text pose /
    activity / where it is) + the scene `context` (date/time/lighting/season/temperature) +
    the locked cartoon art style. Text suppressed unless asked."""
    look = _REFERENCE_STYLE if has_reference else (" " + character.strip())
    feeling = f" Moko's expression right now: {expression}."
    doing = f" {scenario.strip()[0].upper()}{scenario.strip()[1:]}." if scenario.strip() else ""
    # With a reference, its own shading governs — only add the anti-photo guard, not a
    # second (conflicting) positive style statement.
    style = _NOT_PHOTO if has_reference else (_ART_STYLE_POSITIVE + _NOT_PHOTO)
    prompt = _SELFIE_FRAMING + look + feeling + doing + context + _ANATOMY + style
    return suppress_text(prompt, scenario)


def moko_photo_prompt(subject: str, *, context: str = "") -> str:
    """Build a prompt for a one-off photo Moko takes of something it wants to show (not a
    selfie) — a cozy scene, a thing it noticed. Moko's candid first-person phone aesthetic +
    the scene `context` (time/weather/season), in its cartoon style. No character reference
    (Moko itself isn't in it); text suppressed unless asked for."""
    s = subject.strip()
    subj = (s[0].lower() + s[1:]) if s else "something lovely Moko noticed"
    prompt = (f"A candid FIRST-PERSON phone photo Moko took of {subj} — as if Moko is standing "
              f"right there and held its phone up at eye level to snap it: a normal eye-level "
              f"snapshot looking horizontally across the scene (NOT a top-down, isometric, or "
              f"floor-plan view), portrait orientation." + _WORLD_STYLE + context
              + _ART_STYLE_POSITIVE + _NOT_PHOTO)
    return suppress_text(prompt, subject)


def media_error_reason(exc: Exception) -> str:
    """Turn a media-generation failure into a short, useful reason Moko can relay — a quota
    wall, an auth problem, a timeout — instead of a generic 'it didn't work'. Reads the HTTP
    status off an httpx error without importing httpx here."""
    code = getattr(getattr(exc, "response", None), "status_code", None)
    if code == 429:
        return ("the image/AI service hit its rate limit or quota (HTTP 429) — its billing "
                "may not be set up, or the quota's used up for now and needs to reset")
    if code in (401, 403):
        return (f"the service rejected the credentials (HTTP {code}) — the API key may be "
                "invalid or missing access")
    if code == 400:
        return ("the service rejected the request (HTTP 400) — the model id or parameters "
                "may be wrong")
    if isinstance(code, int) and code >= 500:
        return f"the service had a server error (HTTP {code}) — likely temporary, worth a retry"
    if isinstance(code, int):
        return f"the service returned HTTP {code}"
    name = type(exc).__name__
    if "Timeout" in name:
        return "the service timed out"
    if "Connect" in name or "Network" in name:
        return "I couldn't reach the service (a network error)"
    msg = str(exc).strip()
    return msg[:140] if msg else name


# ── the brain-bound media Tool specs ────────────────────────────────────────────
GENERATE_IMAGE_TOOL = Tool(
    name="generate_image",
    description=(
        "Make a general, real picture from a text description — a banner, a diagram, an "
        "illustration, a design, a picture of some thing or scene the companion asks for. It "
        "is a NORMAL image: NOT in your cartoon style, unless the request explicitly asks for "
        "a style. Use this for general/practical/assistant requests — anything that isn't a "
        "picture of you or a scene you're showing. Saved to your media folder; send it with "
        "`send_attachment`. (For a picture of YOURSELF use `self_portrait`; for a photo of a "
        "scene or thing you want to show use `share_photo`.)"
    ),
    parameters={
        "type": "object",
        "properties": {
            "prompt": {"type": "string", "description": "what to draw, described vividly"},
        },
        "required": ["prompt"],
    },
)

SELF_PORTRAIT_TOOL = Tool(
    name="self_portrait",
    description=(
        "Make a picture of YOURSELF — Moko — to show the companion (e.g. when they ask "
        "'send me a selfie' / 'a picture of yourself' / 'show me what you're doing'). It "
        "uses your locked character + reference so you look consistent and on-style, in "
        "portrait orientation, in your current mood. With no `scenario` it's a plain head-on "
        "selfie, looking straight into the camera. Pass `scenario` for what you're DOING and "
        "WHERE (a pose, an activity, a setting) — e.g. 'sipping cocoa by a rainy window'. The "
        "picture has NO text unless the scenario asks for it. Saves it; send it with "
        "`send_attachment`. Sharing a selfie is a SPECIAL, occasional treat — like a friend "
        "sending one now and then, NOT something to do in every message. Only when they ask, "
        "or it genuinely adds to the moment."
    ),
    parameters={
        "type": "object",
        "properties": {
            "scenario": {
                "type": "string",
                "description": (
                    "what you're DOING and WHERE — your pose, activity, and setting (optional; "
                    "leave empty for a plain head-on selfie). E.g. 'waving hello on a sunny "
                    "porch'."
                ),
            },
        },
    },
)

SHARE_PHOTO_TOOL = Tool(
    name="share_photo",
    description=(
        "Take a phone photo of a scene or thing you want to show the companion — a cozy "
        "corner, a view, something you noticed or made — in your candid cartoon phone style. "
        "This is NOT a selfie (use `self_portrait` for a picture of yourself) and NOT a "
        "general/practical image (use `generate_image` for a banner, a diagram, or a design). "
        "Describe the `subject` — what the photo is of, and any detail you want in it (it's a "
        "one-off picture, so put what matters into the description). No text on the image "
        "unless you ask for it. Saves it; send it with `send_attachment`. Sharing a photo is a "
        "SPECIAL, occasional thing — like a friend would now and then, NOT in every message. "
        "Only when they ask, or it genuinely adds to what you're sharing."
    ),
    parameters={
        "type": "object",
        "properties": {
            "subject": {
                "type": "string",
                "description": (
                    "what the photo is OF, described the way you want it to look — e.g. 'a "
                    "steaming mug of cocoa on a windowsill in the rain'."
                ),
            },
        },
        "required": ["subject"],
    },
)

GENERATE_VIDEO_TOOL = Tool(
    name="generate_video",
    description=(
        "Create a short video (a few seconds) from a text description. It's saved to "
        "your media folder; send it with `send_attachment`. Use it when they ask you to "
        "make/animate a short clip. Video generation is slow (up to a few minutes) and "
        "costs money, so it's a gated action — only when clearly asked."
    ),
    parameters={
        "type": "object",
        "properties": {
            "prompt": {
                "type": "string",
                "description": "what happens in the clip, described vividly",
            },
        },
        "required": ["prompt"],
    },
)

SPEAK_TOOL = Tool(
    name="speak",
    description=(
        "Say something out loud in your own voice — turn text into a voice note. It's "
        "saved as an audio file; send it with `send_attachment`. Use it when they ask "
        "you to 'say it', send a voice message, or read something aloud. (Generating "
        "speech costs money, so it's gated.)"
    ),
    parameters={
        "type": "object",
        "properties": {
            "text": {"type": "string", "description": "the words to speak, in your own voice"},
        },
        "required": ["text"],
    },
)
