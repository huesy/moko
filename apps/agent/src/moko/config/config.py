"""The small set of things Moko learns about its companion at setup.

Deliberately tiny and non-technical: who you are, where (when) you are,
how present Moko should be, and what you'd like help with. Richer
knowledge lives in the graph, not here.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

import yaml

from .paths import config_path, ensure_home

logger = logging.getLogger(__name__)

CONFIG_VERSION = 1

# A wall-clock "HH:MM", 24-hour — the one place the sleep-window shape is validated on load
# (mirrors the `moko config set` check so a hand-edited file can't drift from what `set` accepts).
_HHMM = re.compile(r"([01]\d|2[0-3]):[0-5]\d")

# Moko's extra senses beyond the brain (which is `llm_*`). Each can be configured
# with its own provider/model/auth, independently — so a text-only or local brain
# can still have a cloud image model, and vice-versa.
#   vision  — understanding images sent to it. A *chat* capability: empty = use the
#             brain (if it can see); set = an explicit override model for sight.
#   image   — generating images (incl. self-portraits). An independent service.
#   video   — generating short videos (text→video, image→video). Independent.
#   speech  — text→speech (TTS). Independent.
#   hearing — speech→text (ASR). Independent (a local engine keeps audio on-box).
MODALITIES = ("vision", "image", "video", "speech", "hearing")

# How much `moko chat` surfaces, lowest→highest. The single source of truth for the
# verbosity enum — the config validator, the chat fallback, and the TUI picker all derive
# from it, so they can't drift out of agreement (accept a value one path then reject it).
CHAT_VERBOSITY = ("quiet", "normal", "verbose")

# The default text description of Moko's character, prepended to text-to-image prompts so
# the look stays consistent on providers that can't take a reference image (e.g. xAI). It's
# the *character only* (the art style is applied separately); refine it via
# `moko config persona.image_character "…"`. Kept in sync with the bundled reference image.
DEFAULT_IMAGE_CHARACTER = (
    "Moko is a small, round, plump creature whose entire body is covered in soft, shaggy, "
    "fluffy fur in warm vibrant pink shading to a deeper rose-magenta. Its only bare feature "
    "is a small, smooth, cream-coloured face nestled in the fur like a hood opening. The face "
    "is minimal and Baymax-like — just two small black dot eyes and a simple small mouth, and "
    "nothing else on it (no nose, no other features). It has short stubby legs with little "
    "rounded feet, and small stubby arms ending in simple rounded hands, each with one thumb "
    "and three fingers."
)


@dataclass
class ModelChoice:
    """One capability's model: which provider, which model, how to authenticate.
    The secret (API key/token) itself lives in the secrets store, keyed by provider
    — never here."""

    # "anthropic" | "openai" | "xai" | "openai_compatible" | "elevenlabs" | "local" | "none"
    provider: str = "none"
    model: str = ""
    auth: str = ""  # "api_key"|"claude_code"|"oauth"|"env"|"local"|""
    base_url: str = ""  # for openai_compatible / self-hosted endpoints

    @property
    def configured(self) -> bool:
        return self.provider not in ("", "none")

    @classmethod
    def from_dict(cls, data: dict | None) -> ModelChoice:
        d = data or {}
        return cls(
            provider=d.get("provider", "none") or "none",
            model=d.get("model", "") or "",
            auth=d.get("auth", "") or "",
            base_url=d.get("base_url", "") or "",
        )

    def to_dict(self) -> dict:
        out: dict = {"provider": self.provider, "model": self.model}
        if self.auth:
            out["auth"] = self.auth
        if self.base_url:
            out["base_url"] = self.base_url
        return out


@dataclass
class Cadence:
    """The handful of rhythm knobs a companion may reasonably want to tune. Most
    never touch these — the defaults are sensible. The other two cadence controls
    live elsewhere in config already: `proactivity` (how chatty) and the `sleep`
    window (quiet hours). Safety bounds — the reconsider floor/cap, the approval
    TTL, the dedup logic — are deliberately *not* here; they stay code constants.

    This is the source of truth for these values; the agent reads them from here.
    """

    # Quiet required after the last activity (your message or Moko's own ping)
    # before Moko will send an unprompted message — so it never talks over a
    # live or just-ended conversation.
    outreach_settle_minutes: int = 30
    # After Moko muses to itself, how long before it may muse again on unchanged
    # material. A wait-between, not a metronome (it backs off further if musing
    # keeps coming up empty).
    reflect_muse_interval_minutes: int = 30


# Where and how contained Moko runs (see SANDBOX.md). The tier is config, learned at
# setup — never hard-coded.
#   host   — a bare/hardened process on the machine (systemd --user). M7 host-agency intact.
#   docker — inside a Docker container.  podman — inside a Podman container (preferred on
#            Linux: daemonless, rootless, no UID-remap friction on the ~/.moko mount).
RUNTIME_MODES = ("host", "docker", "podman")
# Host-tier hardening posture (the systemd sandbox profile).
#   off    — today's plain unit.  on — system paths protected, $HOME reachable (M7 intact).
#   strict — also hides $HOME except ~/.moko + the companion's chosen `mounts`.
HARDENING_LEVELS = ("off", "on", "strict")


@dataclass
class RuntimeConfig:
    """Moko's runtime tier. Chosen at setup, held here — never baked into code.

    `mode` picks the container backend (or `host`); `hardened` is the host-tier
    systemd sandbox posture. The container knobs (`image`/`container_name`/
    `monitor_port`/`mounts`) only matter when `mode` is a container. `mounts` are
    extra host paths exposed to Moko inside the sandbox — the M7 reach knob.
    """

    mode: str = "host"
    hardened: str = "off"  # off|on|strict — default off so upgrading never silently re-hardens
    image: str = "moko:local"
    container_name: str = "moko"
    monitor_port: int = 4319
    mounts: list[str] = field(default_factory=list)

    @property
    def is_container(self) -> bool:
        return self.mode in ("docker", "podman")

    @property
    def expects_daemon(self) -> bool:
        """True when a live daemon must already be running for a control command to
        reach it (container mode) — so the CLI must NOT fall back to a second
        in-process mind against the shared DB. Host mode may fall back."""
        return self.is_container

    @classmethod
    def from_dict(cls, data: dict | None) -> RuntimeConfig:
        d = data or {}
        mode = d.get("mode", "host") or "host"
        hardened = d.get("hardened", "off") or "off"
        try:
            port = int(d.get("monitor_port", 4319))
        except (TypeError, ValueError):
            port = 4319
        return cls(
            mode=mode if mode in RUNTIME_MODES else "host",
            hardened=hardened if hardened in HARDENING_LEVELS else "off",
            image=d.get("image", "moko:local") or "moko:local",
            container_name=d.get("container_name", "moko") or "moko",
            monitor_port=port,
            mounts=[m for m in (d.get("mounts") or []) if m],
        )

    def to_dict(self) -> dict:
        return {
            "mode": self.mode,
            "hardened": self.hardened,
            "image": self.image,
            "container_name": self.container_name,
            "monitor_port": self.monitor_port,
            "mounts": list(self.mounts),
        }


def _sleep_window(data: dict, new_key: str, legacy_key: str, default: str) -> str:
    """Read one edge of the sleep window, preferring the new `[sleep] start/end` and
    falling back to the legacy `[dream] sleep/wake` so upgrading doesn't reset the window."""
    new = data.get("sleep")
    if isinstance(new, dict) and new.get(new_key):
        return new[new_key]
    legacy = data.get("dream")
    if isinstance(legacy, dict) and legacy.get(legacy_key):
        return legacy[legacy_key]
    return default


# ── load-time normalisers ─────────────────────────────────────────────────────
# A hand-edited config.yaml can hold anything; `Config.from_dict` is the one seam where
# that untrusted YAML becomes a live object. These clamp each field back to a safe value
# (defaulting + warning, never raising) so a typo like `proactivity: loud` degrades
# gracefully instead of surfacing as a crash deep in a later beat. Enum allowed-values are
# reused from their single sources (never re-listed here); `moko config set` keeps its own
# raising validators for the interactive path where a loud error is what you want.


def _clamp_enum(value, allowed, default: str, label: str) -> str:
    """Return `value` if it's an allowed enum member, else the default (with a warning)."""
    if value in allowed:
        return value
    logger.warning("config: invalid %s %r — falling back to %r", label, value, default)
    return default


def _valid_timezone(value, default: str = "UTC") -> str:
    """An IANA name that actually resolves, else the default (with a warning)."""
    from zoneinfo import available_timezones

    if isinstance(value, str) and value in available_timezones():
        return value
    logger.warning("config: unknown timezone %r — falling back to %r", value, default)
    return default


def _valid_hhmm(value, default: str, label: str) -> str:
    """A 24-hour `HH:MM` string, else the default (with a warning)."""
    if isinstance(value, str) and _HHMM.fullmatch(value):
        return value
    logger.warning("config: invalid %s %r — falling back to %r", label, value, default)
    return default


def _posint(value, default: int, label: str) -> int:
    """A whole number > 0, else the default (with a warning). Coerces numeric strings."""
    try:
        n = int(value)
    except (TypeError, ValueError):
        n = 0
    if n > 0:
        return n
    logger.warning("config: %s must be > 0, got %r — falling back to %d", label, value, default)
    return default


@dataclass
class Config:
    """Moko's setup answers, persisted to ~/.moko/config.yaml."""

    companion_name: str
    timezone: str  # IANA name, e.g. "Asia/Tokyo"
    sleep_start: str  # "HH:MM" local — start of the sleep window (Moko dreams within it)
    sleep_end: str  # "HH:MM" local — end of the sleep window (wake)
    proactivity: str  # "gentle" | "moderate" | "chatty"
    # The companion's preferred language (a code like "en"/"ja"/"es"), chosen at setup.
    # Drives the wizard's UI where translated, and the language Moko converses in.
    language: str = "en"
    # The companion's city, for ambient weather (geocoded on the fly). Optional; when
    # blank Moko falls back to a city it has learned about them, else weather stays off.
    location_name: str = ""
    interests: list[str] = field(default_factory=list)
    # How much `moko chat` shows: "quiet" (just the exchange), "normal"
    # (+ a thinking spinner), "verbose" (+ live tool trace + per-turn stats).
    chat_verbosity: str = "normal"
    # Moko's brain — which LLM thinks for it. The API key itself is NOT
    # stored here; it lives in the secrets store (see secrets.py).
    llm_provider: str = "none"  # "anthropic" | "openai" | "openai_compatible" | "none"
    llm_model: str = ""
    llm_auth_method: str = ""  # "api_key" | "claude_code" | "env"
    llm_base_url: str = ""  # for openai_compatible endpoints
    # Per-modality models (M10): vision / image / video / speech / hearing, each its
    # own provider+model+auth, independent of the brain (`llm_*`). See MODALITIES.
    models: dict[str, ModelChoice] = field(default_factory=dict)
    # A locked reference image of Moko's character, so its self-portraits stay
    # on-model across generations. Path is config (companion-placeable), `~` expanded
    # at read time; if absent, the bundled repo copy is used. Only providers that do
    # real reference-guided generation (Gemini, OpenAI) attach it; text-only image APIs
    # (xAI) rely on `image_character` below instead.
    self_portrait_reference: str = "~/moko-character-reference.png"
    # The text "character bible" — a fixed, detailed description of how Moko looks,
    # prepended to every text-to-image generation so the character stays consistent on
    # providers that can't take a reference image. Editable (`moko config
    # persona.image_character "…"`) so it can be refined without a code change.
    image_character: str = DEFAULT_IMAGE_CHARACTER
    # Telegram pairing (the bot token is a secret, kept in secrets.yaml).
    telegram_user_id: str = ""  # the only sender Moko listens to
    telegram_chat_id: str = ""  # where Moko sends (replies + proactive)
    # Who can reach Moko's outward-facing surfaces (the monitor today; future
    # web tools tomorrow). A ladder of increasing reach — see monitor/access.py.
    #   "local"     — only this computer (loopback). Default; no auth needed.
    #   "tailscale" — your own devices over your tailnet. Auth required.
    #   "network"   — other devices on your LAN. Auth required + a firewall rule.
    # The access token itself is a secret (secrets.yaml), never stored here.
    monitor_access: str = "local"
    # Whether Moko may pursue its OWN curiosities on the open web, unprompted, on idle
    # beats (answering its own research-path questions between chats). This ingests
    # untrusted web content with no human watching — a standing prompt-injection surface
    # (a poisoned "fact" can ride into a later turn where sensitive tools are live), so
    # it's OFF unless the companion opts in at setup or via
    # `moko config curiosity.autonomous_research on`. Reactive lookups ("look up X") and
    # private reflection are unaffected — this governs only *unprompted*, autonomous web
    # research. Read live each heartbeat, so a change takes effect without a restart.
    allow_autonomous_research: bool = False
    # User-tunable rhythm knobs (settle/reflection); see Cadence.
    cadence: Cadence = field(default_factory=Cadence)
    # Where and how contained Moko runs (host / hardened host / container); see RuntimeConfig.
    runtime: RuntimeConfig = field(default_factory=RuntimeConfig)
    version: int = CONFIG_VERSION
    created_at: str = ""

    def model_for(self, modality: str) -> ModelChoice:
        """The configured model for a modality (vision/image/speech/hearing), or an
        empty (unconfigured) choice — so callers never KeyError on an unset sense."""
        return self.models.get(modality) or ModelChoice()

    def to_dict(self) -> dict:
        return {
            "version": self.version,
            "created_at": self.created_at,
            "companion": {"name": self.companion_name},
            "language": self.language,
            "timezone": self.timezone,
            "location": self.location_name,
            "sleep": {"start": self.sleep_start, "end": self.sleep_end},
            "proactivity": self.proactivity,
            "interests": list(self.interests),
            "chat": {"verbosity": self.chat_verbosity},
            "llm": {
                "provider": self.llm_provider,
                "model": self.llm_model,
                "auth_method": self.llm_auth_method,
                "base_url": self.llm_base_url,
            },
            "models": {cap: self.model_for(cap).to_dict() for cap in MODALITIES},
            "persona": {
                "self_portrait_reference": self.self_portrait_reference,
                "image_character": self.image_character,
            },
            "telegram": {
                "user_id": self.telegram_user_id,
                "chat_id": self.telegram_chat_id,
            },
            "access": {"monitor": self.monitor_access},
            "curiosity": {"autonomous_research": self.allow_autonomous_research},
            "cadence": {
                "outreach_settle_minutes": self.cadence.outreach_settle_minutes,
                "reflect_muse_interval_minutes": self.cadence.reflect_muse_interval_minutes,
            },
            "runtime": self.runtime.to_dict(),
        }

    @classmethod
    def from_dict(cls, data: dict) -> Config:
        llm = data.get("llm") or {}
        models_data = data.get("models") or {}
        models = {cap: ModelChoice.from_dict(models_data.get(cap)) for cap in MODALITIES}
        persona = data.get("persona") or {}
        telegram = data.get("telegram") or {}
        cadence_data = data.get("cadence") or {}
        defaults = Cadence()
        cadence = Cadence(
            outreach_settle_minutes=_posint(
                cadence_data.get("outreach_settle_minutes", defaults.outreach_settle_minutes),
                defaults.outreach_settle_minutes,
                "cadence.outreach_settle_minutes",
            ),
            reflect_muse_interval_minutes=_posint(
                cadence_data.get(
                    "reflect_muse_interval_minutes", defaults.reflect_muse_interval_minutes
                ),
                defaults.reflect_muse_interval_minutes,
                "cadence.reflect_muse_interval_minutes",
            ),
        )
        # Allowed enum members, pulled live from their single sources of truth so this
        # can't drift from what the wizard/config-editor accept. `catalog` is imported
        # lazily because it imports this module — a top-level import would be circular.
        from ..cli.catalog import PROACTIVITY
        from ..i18n import LANGUAGES

        proactivity_values = {v for v, _ in PROACTIVITY}
        language_codes = {code for code, _, _ in LANGUAGES}
        return cls(
            companion_name=(data.get("companion") or {}).get("name", ""),
            language=_clamp_enum(
                data.get("language", "en") or "en", language_codes, "en", "language"
            ),
            timezone=_valid_timezone(data.get("timezone", "UTC")),
            location_name=data.get("location", "") or "",
            # The sleep window. New configs use [sleep] start/end; older ones used
            # [dream] sleep/wake — read both so an existing install isn't reset on upgrade.
            sleep_start=_valid_hhmm(
                _sleep_window(data, "start", "sleep", "23:00"), "23:00", "sleep.start"
            ),
            sleep_end=_valid_hhmm(
                _sleep_window(data, "end", "wake", "07:00"), "07:00", "sleep.end"
            ),
            proactivity=_clamp_enum(
                data.get("proactivity", "moderate"), proactivity_values, "moderate", "proactivity"
            ),
            interests=list(data.get("interests") or []),
            chat_verbosity=_clamp_enum(
                (data.get("chat") or {}).get("verbosity", "normal"),
                CHAT_VERBOSITY,
                "normal",
                "chat.verbosity",
            ),
            llm_provider=llm.get("provider", "none"),
            llm_model=llm.get("model", ""),
            llm_auth_method=llm.get("auth_method", ""),
            llm_base_url=llm.get("base_url", "") or "",
            models=models,
            self_portrait_reference=persona.get(
                "self_portrait_reference", "~/moko-character-reference.png"
            ),
            image_character=persona.get("image_character") or DEFAULT_IMAGE_CHARACTER,
            telegram_user_id=telegram.get("user_id", "") or "",
            telegram_chat_id=telegram.get("chat_id", "") or "",
            monitor_access=(data.get("access") or {}).get("monitor", "local"),
            allow_autonomous_research=bool(
                (data.get("curiosity") or {}).get("autonomous_research", False)
            ),
            cadence=cadence,
            runtime=RuntimeConfig.from_dict(data.get("runtime")),
            version=data.get("version", CONFIG_VERSION),
            created_at=data.get("created_at", ""),
        )

    def save(self):
        """Write the config to ~/.moko/config.yaml; return the path."""
        ensure_home()
        path = config_path()
        with path.open("w", encoding="utf-8") as fh:
            yaml.safe_dump(self.to_dict(), fh, sort_keys=False, allow_unicode=True)
        return path


def exists() -> bool:
    """Has Moko been set up?"""
    return config_path().exists()


def load() -> Config | None:
    """Read the config, or None if Moko hasn't been set up yet."""
    path = config_path()
    if not path.exists():
        return None
    with path.open(encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    return Config.from_dict(data)
