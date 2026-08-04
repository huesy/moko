"""The full-screen `moko config` navigator — a keyboard-driven master-detail.

Categories down the left, that category's options on the right. ↑/↓ move,
→/ENTER drill in (category → its options → edit one), ←/ESC/Backspace step back
out (and ESC/Backspace quit from the top level), Ctrl+C quits anywhere.

*Everything* is edited in-frame — including the flows that used to drop out to
the line-based prompts: choosing a timezone (type-to-filter over every zone),
choosing a Brain (provider → model → sign-in, in steps), and changing dashboard
Access (tier → an in-frame panel with the URL, password, and any firewall line,
applied for you). The companion never leaves the full-screen view.

Built in-house on :mod:`rich`'s `Live` (alternate screen, flicker-free) and the
terminal-native key decoder in :mod:`moko.tui.keys`, to match the rest of the
CLI's small-dependency, terminal-native style — no Textual, no new deps.
"""

from __future__ import annotations

import shutil
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from rich.console import Group
from rich.live import Live
from rich.panel import Panel
from rich.text import Text

from .. import i18n
from ..config import config as cfg
from ..config.providers import MODALITY_DEFAULT_MODEL
from ..theme import MOKO_PINK, MOKO_PINK_DIM, console
from ..tui import keys as K
from . import catalog
from ._tui import (
    _VIM,
    CONTEXT,
    DIVIDER,
    ESCAPE,
    GOOD,
    HILITE,
    MUTED,
    WARN,
    CheckEditor,
    InfoEditor,
    ListEditor,
    Terminal,
    TextEditor,
)
from ._tui import http_url as _http_url
from ._tui import required as _required

# ── composite editors: the flows that used to drop out to questionary ─────────


def _key_editor(provider: str) -> TextEditor:
    """The paste-a-key field, shared by the Brain and per-sense flows. When a key for
    this provider is already saved, an empty entry keeps it (Enter = leave it as is);
    otherwise a non-empty key is required."""
    from ..config import secrets
    if secrets.get_api_key(provider):
        return TextEditor("Paste a new API key — or press Enter to keep the saved one",
                          mask=True)
    return TextEditor("Paste your API key", mask=True, validate=_required("an API key"))


class BrainEditor:
    """Provider → model → sign-in, walked in steps, all in-frame. Persists to
    config (and secrets) itself on the final step."""

    def __init__(self, config: cfg.Config) -> None:
        self.config = config
        self.cat = catalog
        self.d: dict[str, Any] = {"base_url": "", "api_key": None}
        self.step = "provider"
        self.sub = ListEditor(
            "Which AI provider should do my thinking?",
            [(label, value) for value, label in catalog.LLM_PROVIDERS],
            config.llm_provider,
        )

    def handle(self, key: K.Key):
        result = self.sub.handle(key)
        if result is None:
            return None
        action, value = result
        if action == "cancel":
            return ("cancel", None)  # esc at any step abandons the whole edit
        return self._advance(value)

    def _go(self, step: str, sub):
        self.step, self.sub = step, sub
        return None

    def _advance(self, value):
        s = self.cat
        if self.step == "provider":
            self.d["provider"] = value
            if value == "none":
                return self._finish()
            if value == "openai_compatible":
                return self._go("base_url", TextEditor("What's the API endpoint URL?",
                                                       self.config.llm_base_url,
                                                       validate=_http_url))
            return self._go("model", self._model_editor(value))
        if self.step == "base_url":
            self.d["base_url"] = value.strip()
            return self._go("model", TextEditor("And which model?", self.config.llm_model,
                                                validate=_required("a model name")))
        if self.step == "model":
            if self.d["provider"] == "xai" and value == "__other__":
                return self._go("model_text", TextEditor("What's the model name?",
                                                         validate=_required("a model name")))
            self.d["model"] = value.strip() if isinstance(value, str) else value
            if self.d["provider"] == "xai":
                self.d["base_url"] = s.XAI_BASE_URL
            return self._go("auth", self._auth_editor(self.d["provider"]))
        if self.step == "model_text":
            self.d["model"] = value.strip()
            self.d["base_url"] = s.XAI_BASE_URL
            return self._go("auth", self._auth_editor(self.d["provider"]))
        if self.step == "auth":
            if value == "keep":  # reuse the key already on file — nothing to paste
                self.d["auth"] = "api_key"
                return self._finish()
            self.d["auth"] = value
            if value == "api_key":
                return self._go("key", _key_editor(self.d["provider"]))
            if value == "oauth":
                self._persist()
                return self._go("info", InfoEditor("Almost there", [
                    "When you close config I'll connect your Grok subscription —",
                    "a quick browser approval, nothing to paste. (Or run `moko login`.)",
                ]))
            return self._finish()
        if self.step == "key":
            if value.strip():  # blank → keep the saved key (don't overwrite it)
                self.d["api_key"] = value.strip()
            return self._finish()
        if self.step == "info":
            return ("commit", None)  # already persisted before the panel
        return None

    def _model_editor(self, provider: str):
        s = self.cat
        if provider == "anthropic":
            return ListEditor("Which Claude model?",
                              [(label, value) for value, label in s.ANTHROPIC_MODELS],
                              self.config.llm_model or "claude-opus-4-8")
        if provider == "xai":
            return ListEditor("Which Grok model?",
                              [(label, value) for value, label in s.XAI_MODELS],
                              self.config.llm_model or "grok-4.3")
        return TextEditor("Which model? (the model name from your account)",
                          self.config.llm_model, validate=_required("a model name"))

    def _auth_editor(self, provider: str):
        import os
        import shutil

        from ..config import secrets
        s = self.cat
        choices: list[tuple[str, str]] = []
        recommended = False
        if provider == "anthropic" and shutil.which("claude"):
            choices.append(("Use Claude Code on this machine (no key needed) — recommended",
                            "claude_code"))
            recommended = True
        if provider == "xai":
            choices.append(("Sign in with my subscription (OAuth) — recommended", "oauth"))
            recommended = True
        env_var = s.ENV_VARS.get(provider)
        if env_var and os.environ.get(env_var):
            choices.append((f"Use {env_var} already set in my environment", "env"))
        current = self.config.llm_auth_method
        if secrets.get_api_key(provider):
            # A key for this provider is already saved — keep it without re-pasting.
            label = "Keep the API key I already saved"
            choices.append((label if recommended else label + " — recommended", "keep"))
            choices.append(("Paste a different API key", "api_key"))
            if current in ("", "api_key"):
                current = "keep"  # pre-highlight the no-friction default
        else:
            choices.append(("Paste an API key", "api_key"))
        return ListEditor("How should I sign in?", choices, current)

    def _persist(self) -> None:
        from ..config import secrets
        self.config.llm_provider = self.d["provider"]
        self.config.llm_model = self.d.get("model", "")
        self.config.llm_auth_method = self.d.get("auth", "")
        self.config.llm_base_url = self.d.get("base_url", "")
        self.config.save()
        if self.d.get("api_key"):
            secrets.set_api_key(self.d["provider"], self.d["api_key"])

    def _finish(self):
        self._persist()
        return ("commit", None)

    def render(self, width: int) -> list[Text]:
        return self.sub.render(width)

    @property
    def hint(self) -> str:
        return self.sub.hint


class AccessEditor:
    """Pick a dashboard-access tier, then show the resulting URL / password /
    firewall line in-frame and apply it (token + service restart)."""

    def __init__(self, config: cfg.Config) -> None:
        from ..monitor import access
        self.config = config
        self.access = access
        self.step = "tier"
        self.sub = ListEditor(
            "Who can reach Moko's dashboard?",
            [(label, value) for value, label, _ in access.TIER_CHOICES],
            config.monitor_access,
        )

    def handle(self, key: K.Key):
        result = self.sub.handle(key)
        if result is None:
            return None
        action, value = result
        if action == "cancel":
            return ("cancel", None)
        if self.step == "tier":
            self.config.monitor_access = value
            self.config.save()
            self.step = "info"
            self.sub = InfoEditor("Dashboard access 🌸", self._apply(value))
            return None
        return ("commit", None)  # info panel dismissed

    def _apply(self, tier: str) -> list[str]:
        access = self.access
        lines: list[str] = []
        token = access.ensure_token() if access.requires_auth(tier) else ""
        _host, problem = access.resolve_host(tier)
        url = access.reachable_url(tier, access.MONITOR_PORT)
        lines.append(f"Open the dashboard at: {url}")
        if token:
            lines.append(f"Password (once per device): {token}")
        if problem:
            lines.append(f" {problem}")
        if tier == "network":
            lines.append(" One-time — open the port on your firewall by running:")
            lines.append(f"   {access.lan_firewall_command()}")
        lines.append(self._restart())
        return lines

    def _restart(self) -> str:
        from . import service
        if not service.service_state().get("active"):
            return " Restart Moko for this to take effect: moko start"
        return (" Restarted Moko to apply. 🌸" if service.restart_quiet()
                else " Restart didn't hold — check `moko service logs`.")

    def render(self, width: int) -> list[Text]:
        return self.sub.render(width)

    @property
    def hint(self) -> str:
        return self.sub.hint


# The per-modality senses (M10). Vision is a chat capability — empty means "use the
# brain if it can see"; the rest are independent services that are simply off until set.
_MODALITY_PROMPT = {
    "vision": "Which model should look at images sent to you?",
    "image": "Which model should make images (incl. pictures of yourself)?",
    "video": "Which model should make short videos?",
    "speech": "Which model should give you a voice (text-to-speech)?",
    "hearing": "Which model should transcribe what you hear (speech-to-text)?",
}
_MODALITY_PROVIDERS = {
    "vision": [("Use my brain, if it can see", "none"), ("OpenAI", "openai"),
               ("Anthropic", "anthropic"),
               ("Another OpenAI-compatible endpoint", "openai_compatible")],
    "image": [("Not set up", "none"), ("OpenAI — gpt-image-1", "openai"),
              ("Grok Imagine (xAI)", "xai"), ("Google Gemini", "google")],
    "video": [("Not set up", "none"), ("Grok Imagine Video (xAI)", "xai")],
    "speech": [("Not set up", "none"), ("OpenAI — gpt-4o-mini-tts", "openai"),
               ("Grok voice (xAI)", "xai")],
    "hearing": [("Not set up", "none"), ("OpenAI — whisper", "openai"),
                ("Grok hearing (xAI)", "xai")],
}
# (modality, provider) pairs whose endpoint has no model id to pick — skip that step. The
# default-model ids themselves come from the shared catalog (config.providers).
_MODALITY_NO_MODEL = {("speech", "xai"), ("hearing", "xai")}


def _modality_label(config: cfg.Config, modality: str) -> str:
    mc = config.model_for(modality)
    if not mc.configured:
        return "use brain" if modality == "vision" else "—"
    return f"{mc.provider} · {mc.model}" if mc.model else mc.provider


class ModalityEditor:
    """Provider → model → sign-in for one sense (vision/image/speech/hearing),
    walked in steps in-frame, persisting to config + secrets itself. Mirrors
    BrainEditor; a 'none'/use-brain pick clears the slot. The API key is captured
    here (into secrets), not stored in config."""

    def __init__(self, config: cfg.Config, modality: str) -> None:
        self.config = config
        self.modality = modality
        self.d: dict[str, Any] = {"base_url": "", "api_key": None}
        self.step = "provider"
        cur = config.model_for(modality)
        self.sub = ListEditor(
            _MODALITY_PROMPT[modality], _MODALITY_PROVIDERS[modality], cur.provider,
        )

    def handle(self, key: K.Key):
        result = self.sub.handle(key)
        if result is None:
            return None
        action, value = result
        if action == "cancel":
            return ("cancel", None)
        return self._advance(value)

    def _go(self, step: str, sub):
        self.step, self.sub = step, sub
        return None

    def _after_model(self, cur):
        """After the model is known (or skipped), reuse a present credential or ask how
        to sign in. Keeping the same provider whose credential is ACTUALLY present → no
        re-entry. (Trusting cur.auth alone would persist auth='api_key' with no key if
        the secret was since deleted — a runtime auth failure waiting to happen.)"""
        from ..config import secrets
        if self.d["provider"] == cur.provider:
            if cur.auth == "oauth" and secrets.get_oauth_tokens(self.d["provider"]):
                self.d["auth"] = "oauth"
                return self._finish()
            if secrets.get_api_key(self.d["provider"]):
                self.d["auth"] = cur.auth or "api_key"
                return self._finish()
        return self._go("auth", self._auth_editor(self.d["provider"]))

    def _advance(self, value):
        cur = self.config.model_for(self.modality)
        if self.step == "provider":
            self.d["provider"] = value
            if value == "none":
                return self._finish()  # clears the slot (vision → use brain)
            if value == "openai_compatible":
                return self._go("base_url", TextEditor("What's the API endpoint URL?",
                                                       cur.base_url, validate=_http_url))
            if (self.modality, value) in _MODALITY_NO_MODEL:
                self.d["model"] = ""  # this endpoint takes no model id (e.g. xAI /tts)
                return self._after_model(cur)
            default = MODALITY_DEFAULT_MODEL.get(self.modality, {}).get(value, "") or cur.model
            return self._go("model", TextEditor("Which model?", default,
                                                validate=_required("a model name")))
        if self.step == "base_url":
            self.d["base_url"] = value.strip()
            return self._go("model", TextEditor("And which model?", cur.model,
                                                validate=_required("a model name")))
        if self.step == "model":
            self.d["model"] = value.strip()
            return self._after_model(cur)
        if self.step == "auth":
            if value == "keep":  # reuse the key already on file — nothing to paste
                self.d["auth"] = "api_key"
                return self._finish()
            self.d["auth"] = value
            if value == "api_key":
                return self._go("key", _key_editor(self.d["provider"]))
            if value == "oauth":  # the Grok subscription — connected on the way out
                self._persist()
                return self._go("info", InfoEditor("Almost there 🌸", [
                    "When you close config I'll connect your Grok subscription (a quick",
                    "browser approval) — the same one your brain can use, no separate key.",
                ]))
            return self._finish()
        if self.step == "key":
            if value.strip():  # blank → keep the saved key (don't overwrite it)
                self.d["api_key"] = value.strip()
            return self._finish()
        if self.step == "info":
            return ("commit", None)  # already persisted before the panel
        return None

    def _auth_editor(self, provider: str):
        import os

        from ..config import secrets
        choices: list[tuple[str, str]] = []
        recommended = False
        if provider == "xai":  # the Grok subscription powers images too (same as the brain)
            choices.append(("Use my Grok subscription (OAuth) — recommended", "oauth"))
            recommended = True
        env_var = catalog.ENV_VARS.get(provider)
        if env_var and os.environ.get(env_var):
            choices.append((f"Use {env_var} from my environment", "env"))
        current = self.config.model_for(self.modality).auth or "api_key"
        if secrets.get_api_key(provider):
            # A key for this provider is already saved (set earlier, or shared by another
            # sense) — keep it without re-pasting.
            label = "Keep the API key I already saved"
            choices.append((label if recommended else label + " — recommended", "keep"))
            choices.append(("Paste a different API key", "api_key"))
            if current == "api_key":
                current = "keep"  # pre-highlight the no-friction default
        else:
            choices.append(("Paste an API key", "api_key"))
        return ListEditor("How should I sign in?", choices, current)

    def _persist(self) -> None:
        from ..config import secrets
        self.config.models[self.modality] = cfg.ModelChoice(
            provider=self.d["provider"], model=self.d.get("model", ""),
            auth=self.d.get("auth", ""), base_url=self.d.get("base_url", ""),
        )
        self.config.save()
        if self.d.get("api_key"):
            secrets.set_api_key(self.d["provider"], self.d["api_key"])

    def _finish(self):
        self._persist()
        return ("commit", None)

    def render(self, width: int) -> list[Text]:
        return self.sub.render(width)

    @property
    def hint(self) -> str:
        return self.sub.hint


Editor = (ListEditor | CheckEditor | TextEditor | InfoEditor | BrainEditor | AccessEditor
          | ModalityEditor)


# ── the field/category model ─────────────────────────────────────────────────


@dataclass
class Field:
    key: str
    label: str
    summary: Callable[[cfg.Config], str]
    make_editor: Callable[[cfg.Config], Editor]
    # Simple fields hand their value back for the navigator to apply + save;
    # composite editors (Brain/Access) persist themselves and leave this None.
    apply: Callable[[cfg.Config, Any], None] | None = None


@dataclass
class Category:
    name: str
    fields: list[Field] = field(default_factory=list)


def _minutes(presets: tuple[int, ...], current: int) -> list[tuple[str, int]]:
    """'N minutes' choices, always including the current value (even if it was
    hand-edited in config.yaml) so the list can pre-highlight it."""
    return [(f"{m} minutes", m) for m in sorted({*presets, current})]


def _timezone_choices() -> list[tuple[str, str]]:
    from zoneinfo import available_timezones
    return [(z, z) for z in sorted(available_timezones())]


def _timezone_preview(zone: str) -> str:
    """The local time in the highlighted zone — Moko is always time-aware, so the
    right zone is the one where the clock matches your wrist."""
    from datetime import datetime
    from zoneinfo import ZoneInfo

    try:
        now = datetime.now(ZoneInfo(zone))
    except Exception:
        return ""
    return f"🕐 it's {catalog.format_time_label(now.strftime('%H:%M'))} there now"


def _build_model() -> list[Category]:
    """The categories and fields, in display order."""

    def times(opts: list[str]) -> list[tuple[str, str]]:
        return [(catalog.format_time_label(t), t) for t in opts]

    def flip(pairs):
        return [(label, value) for value, label in pairs]

    return [
        Category("Identity", [
            Field("name", "Name", lambda c: c.companion_name or "—",
                  lambda c: TextEditor("What should I call you?", c.companion_name),
                  apply=lambda c, v: setattr(c, "companion_name", v) if v else None),
            Field("language", "Language", lambda c: i18n.language_name(c.language),
                  lambda c: ListEditor("Which language should we speak?",
                                       i18n.language_choices(), c.language),
                  apply=lambda c, v: setattr(c, "language", v)),
            Field("timezone", "Timezone", lambda c: c.timezone,
                  lambda c: ListEditor("Your timezone", _timezone_choices(), c.timezone,
                                       searchable=True, preview=_timezone_preview),
                  apply=lambda c, v: setattr(c, "timezone", v)),
            Field("interests", "Interests",
                  lambda c: f"{len(c.interests)} selected" if c.interests else "—",
                  lambda c: CheckEditor("What would you like help with?",
                                        flip(catalog.INTERESTS), c.interests),
                  apply=lambda c, v: setattr(c, "interests", v)),
        ]),
        Category("Rhythm", [
            Field("sleep", "Wind down", lambda c: catalog.format_time_label(c.sleep_start),
                  lambda c: ListEditor("When should Moko wind down for the night?",
                                       times(catalog.SLEEP_TIMES), c.sleep_start),
                  apply=lambda c, v: setattr(c, "sleep_start", v)),
            Field("wake", "Wake up", lambda c: catalog.format_time_label(c.sleep_end),
                  lambda c: ListEditor("And wake up?", times(catalog.WAKE_TIMES), c.sleep_end),
                  apply=lambda c, v: setattr(c, "sleep_end", v)),
            Field("presence", "Presence", lambda c: c.proactivity,
                  lambda c: ListEditor("How present should Moko be?",
                                       flip(catalog.PROACTIVITY), c.proactivity),
                  apply=lambda c, v: setattr(c, "proactivity", v)),
            Field("research", "Self-research",
                  lambda c: "On" if c.allow_autonomous_research else "Off",
                  lambda c: ListEditor(
                      "May Moko look things up on its own when it's curious?",
                      [("Yes — quietly research on its own time", True),
                       ("No — only look things up when I ask", False)],
                      c.allow_autonomous_research),
                  apply=lambda c, v: setattr(c, "allow_autonomous_research", v)),
            Field("settle", "Reach-out settle",
                  lambda c: f"{c.cadence.outreach_settle_minutes} min",
                  lambda c: ListEditor("After you go quiet, how long before Moko might reach out?",
                                       _minutes((15, 30, 45, 60, 90),
                                                c.cadence.outreach_settle_minutes),
                                       c.cadence.outreach_settle_minutes),
                  apply=lambda c, v: setattr(c.cadence, "outreach_settle_minutes", v)),
            Field("muse", "Muse cadence",
                  lambda c: f"{c.cadence.reflect_muse_interval_minutes} min",
                  lambda c: ListEditor("How often may Moko pause to muse when things are quiet?",
                                       _minutes((15, 30, 60, 120),
                                                c.cadence.reflect_muse_interval_minutes),
                                       c.cadence.reflect_muse_interval_minutes),
                  apply=lambda c, v: setattr(c.cadence, "reflect_muse_interval_minutes", v)),
            Field("verbosity", "Chat verbosity", lambda c: c.chat_verbosity,
                  lambda c: ListEditor("How much should `moko chat` show?",
                                       [(_VERBOSITY_LABELS[v], v) for v in cfg.CHAT_VERBOSITY],
                                       c.chat_verbosity),
                  apply=lambda c, v: setattr(c, "chat_verbosity", v)),
        ]),
        Category("Brain", [
            Field("model", "AI model", catalog.brain_label, BrainEditor),
        ]),
        Category("Senses", [
            Field("vision", "Vision (see images)", lambda c: _modality_label(c, "vision"),
                  lambda c: ModalityEditor(c, "vision")),
            Field("image", "Image generation", lambda c: _modality_label(c, "image"),
                  lambda c: ModalityEditor(c, "image")),
            Field("video", "Video generation", lambda c: _modality_label(c, "video"),
                  lambda c: ModalityEditor(c, "video")),
            Field("speech", "Voice (text-to-speech)", lambda c: _modality_label(c, "speech"),
                  lambda c: ModalityEditor(c, "speech")),
            Field("hearing", "Hearing (speech-to-text)", lambda c: _modality_label(c, "hearing"),
                  lambda c: ModalityEditor(c, "hearing")),
            Field("portrait_ref", "Self-portrait reference",
                  lambda c: c.self_portrait_reference or "—",
                  lambda c: TextEditor("Path to a reference image of your character:",
                                       c.self_portrait_reference),
                  apply=lambda c, v: setattr(c, "self_portrait_reference", v) if v else None),
        ]),
        Category("Access", [
            Field("tier", "Dashboard",
                  lambda c: _access_label(c.monitor_access), AccessEditor),
        ]),
        Category("Telegram", [
            Field("user_id", "Paired user id", lambda c: c.telegram_user_id or "—",
                  lambda c: TextEditor("Telegram user id Moko listens to (set by `moko pair`):",
                                       c.telegram_user_id),
                  apply=lambda c, v: setattr(c, "telegram_user_id", v.strip())),
            Field("chat_id", "Reply chat id", lambda c: c.telegram_chat_id or "—",
                  lambda c: TextEditor("Telegram chat id Moko replies to:", c.telegram_chat_id),
                  apply=lambda c, v: setattr(c, "telegram_chat_id", v.strip())),
        ]),
    ]


# Labels only — the values + their order come from cfg.CHAT_VERBOSITY (single source).
_VERBOSITY_LABELS = {
    "quiet": "Quiet — just the conversation",
    "normal": "Normal — a gentle thinking indicator (recommended)",
    "verbose": "Verbose — show tool use and per-turn stats",
}


def _access_label(tier: str) -> str:
    from ..monitor import access
    return access.TIER_LABELS.get(tier, tier)


# ── navigator ────────────────────────────────────────────────────────────────


def _finish_pending_logins(config: cfg.Config) -> None:
    """After the full-screen UI tears down, finish any Grok subscription sign-in the
    config now calls for but that isn't connected yet — inline, on the bare console
    (the device flow can't run inside the Live view). Asks first; declining is fine."""
    from . import login
    uses_oauth = (
        (config.llm_provider == "xai" and config.llm_auth_method == "oauth")
        or any(
            config.model_for(cap).provider == "xai" and config.model_for(cap).auth == "oauth"
            for cap in cfg.MODALITIES
        )
    )
    if not uses_oauth or login.xai_status() == "connected":
        return
    import questionary

    from ..theme import QUESTIONARY_STYLE
    if questionary.confirm(
        "Connect your Grok subscription now?", default=True, style=QUESTIONARY_STYLE
    ).ask():
        login.connect_xai()


def run(config: cfg.Config) -> None:
    """Open the config editor, then finish any pending Grok sign-in on the way out
    (the device flow needs the bare console, after the full-screen UI tears down)."""
    _navigate(config)
    _finish_pending_logins(config)


def _navigate(config: cfg.Config) -> None:
    """Drive the full-screen navigator until the companion quits."""
    model = _build_model()
    cat_idx = 0
    field_idx = 0
    focus = "categories"  # "categories" | "fields" | "edit"
    editor: Editor | None = None
    active_field: Field | None = None
    saved_note = ""  # footer confirmation after a save
    show_help = False
    online = _service_online()  # whether the background service is running, for hints
    pending = False  # a saved change the running service hasn't picked up yet

    with Terminal() as term, Live(console=console, screen=True, auto_refresh=False) as live:
        while True:
            live.update(_render(config, model, cat_idx, field_idx, focus, editor,
                                online=online, saved_note=saved_note, show_help=show_help,
                                pending=pending))
            live.refresh()
            key = term.read_key()

            if key.kind in (K.INTERRUPT, K.EOF):
                return
            if show_help:  # any key dismisses the help overlay
                show_help = False
                continue
            if focus != "edit" and key.kind == K.TEXT and key.text == "?":
                show_help = True
                continue
            if focus != "edit" and key.kind == K.TEXT and key.text == "r":
                if not online:
                    saved_note = "Moko isn't running here — start it with `moko start`"
                else:  # show "restarting…" before the blocking call, then the result
                    live.update(_render(config, model, cat_idx, field_idx, focus, editor,
                                        online=online, saved_note="restarting Moko…",
                                        restarting=True))
                    live.refresh()
                    online = _service_restart()
                    pending = False
                    saved_note = (f"restarted · {_now_label(config)}" if online
                                  else "restart didn't hold — see `moko service logs`")
                continue

            if focus == "edit":
                result = editor.handle(key)
                if result is None:
                    continue
                action, value = result
                if action == "commit":
                    if active_field.apply is not None:
                        active_field.apply(config, value)
                        config.save()
                    saved_note = f"saved · {_now_label(config)}"
                    # Every setting is read once at boot, so a change needs a
                    # restart to go live — except Access, which applies itself.
                    if online:
                        pending = active_field.key != "tier"
                editor = active_field = None
                focus = "fields"
                continue

            if key.kind == K.TEXT and key.text in _VIM:  # vim movement in list views
                key = K.Key(_VIM[key.text])

            fields = model[cat_idx].fields
            if focus == "categories":
                if key.kind == K.UP:
                    cat_idx = (cat_idx - 1) % len(model)
                elif key.kind == K.DOWN:
                    cat_idx = (cat_idx + 1) % len(model)
                elif key.kind in (K.ENTER, K.RIGHT):
                    focus, field_idx = "fields", 0
                elif key.kind in (ESCAPE, K.BACKSPACE):
                    if pending and online:  # apply everything in one restart on the way out
                        live.update(_render(config, model, cat_idx, field_idx, focus, editor,
                                            online=online, pending=pending, restarting=True,
                                            saved_note="applying your changes — restarting Moko…"))
                        live.refresh()
                        _service_restart()
                    return
            elif focus == "fields":
                if key.kind == K.UP:
                    field_idx = (field_idx - 1) % len(fields)
                elif key.kind == K.DOWN:
                    field_idx = (field_idx + 1) % len(fields)
                elif key.kind in (ESCAPE, K.BACKSPACE, K.LEFT):
                    focus = "categories"
                elif key.kind in (K.ENTER, K.RIGHT):
                    active_field = fields[field_idx]
                    editor = active_field.make_editor(config)
                    focus = "edit"


def _service_online() -> bool:
    """Whether Moko's background service is currently running (best-effort)."""
    from . import service
    try:
        return bool(service.service_state().get("active"))
    except Exception:
        return False


def _service_restart() -> bool:
    """Restart the running service in-place (no console output). True if up after."""
    from . import service
    try:
        return service.restart_quiet()
    except Exception:
        return False


def _now_label(config: cfg.Config) -> str:
    from ..clock import Clock
    return catalog.format_time_label(Clock(config.timezone).now().strftime("%H:%M"))


# ── rendering ────────────────────────────────────────────────────────────────


_PAD_X = 2  # Panel horizontal padding (must match the Panel(padding=) below)
_GUTTER = " │ "  # sidebar/content separator — also the full-height divider
_LABEL_W = 18


def _render(config, model, cat_idx, field_idx, focus, editor, *,
            online: bool = False, saved_note: str = "", show_help: bool = False,
            pending: bool = False, restarting: bool = False) -> Panel:
    """Compose each visual line to an exact width by hand — sidebar segment,
    divider, then the content cell — so rich never has to wrap. Cell-aware
    truncation (Text.truncate) keeps emoji and wide glyphs from overflowing."""
    size = shutil.get_terminal_size((80, 24))
    width, height = size.columns, size.lines
    if show_help:
        return _help_panel(height)
    inner = max(24, width - 2 - 2 * _PAD_X)  # panel content width (borders + padding)
    # " › name" plus a couple of cells of breathing room before the divider.
    sidebar_w = max(len(c.name) for c in model) + 5
    content_w = max(12, inner - sidebar_w - len(_GUTTER))

    sidebar = _sidebar(model, cat_idx, focus, sidebar_w)
    if focus == "edit":
        content = editor.render(content_w)
        hint = editor.hint
    else:
        content = [_field_line(fld, config, content_w, active=focus == "fields" and i == field_idx)
                   for i, fld in enumerate(model[cat_idx].fields)]
        hint = ("↑↓ move · →/⏎ edit · ←/esc back" if focus == "fields"
                else "↑↓ move · →/⏎ open · esc quit")

    rows = max(len(sidebar), len(content))
    lines = []
    for r in range(rows):
        left = sidebar[r] if r < len(sidebar) else Text(" " * sidebar_w)
        cell = (content[r] if r < len(content) else Text("")).copy()
        cell.truncate(content_w, overflow="ellipsis", pad=True)  # cell-aware, no wrap
        # Build the line with NO base style of its own; each segment carries its
        # style as a contained span, so a highlighted sidebar item can't bleed
        # pink across the divider into the content area.
        line = Text()
        line.append_text(left)
        line.append(_GUTTER, style=DIVIDER)
        line.append_text(cell)
        lines.append(line)

    if restarting:  # blocked in the (synchronous) restart — don't claim "online"
        dot = f"[{WARN}]◐[/] restarting…"
    elif online:
        dot = f"[{GOOD}]●[/] online"
    else:
        dot = "[muted]○ offline[/]"
    title = f"[accent]moko settings 🌸[/accent]   {dot}"
    if pending and online and not restarting:  # a saved change not yet applied
        title += f"   [{WARN}]⟳ restart pending[/]"
    keys = "? help" + ("  ·  r restart" if online and focus != "edit" else "")
    sub = f"[muted]{hint}  ·  {keys}[/muted]"
    if saved_note:
        mark, colour = ("✓", GOOD) if saved_note.startswith("saved") else ("⚠", WARN)
        sub += f"    [{colour}]{mark} {saved_note}[/]"

    # height fills the terminal so the pink border reaches the very bottom.
    return Panel(Group(*lines), title=title, border_style="moko",
                 subtitle=sub, subtitle_align="left", padding=(1, _PAD_X), height=height)


def _help_panel(height: int) -> Panel:
    rows = [
        ("↑ ↓   ·   j k", "move"),
        ("→  ·  l  ·  Enter", "open / edit"),
        ("←  ·  h  ·  Esc / Backspace", "back  ·  quit at the top (applies your changes)"),
        ("type", "filter a long list (e.g. timezone)"),
        ("Space", "toggle (interests)"),
        ("r", "restart Moko now (apply changes)"),
        ("?", "this help"),
        ("Ctrl+C", "quit without applying"),
    ]
    key_w = max(len(k) for k, _ in rows)
    lines = [Text(""), Text("  Keys", style=CONTEXT), Text("")]
    for k, desc in rows:
        row = Text(f"   {k:<{key_w}}    ", style=f"bold {MOKO_PINK}")
        row.append(desc, style=MUTED)
        lines.append(row)
    return Panel(Group(*lines), title="[accent]moko settings · help 🌸[/accent]",
                 border_style="moko", subtitle="[muted]any key to close[/muted]",
                 subtitle_align="left", padding=(1, _PAD_X), height=height)


def _sidebar(model, cat_idx, focus, sidebar_w) -> list[Text]:
    out = []
    for i, cat in enumerate(model):
        current = i == cat_idx
        if not current:
            style = MUTED
        elif focus == "categories":
            style = HILITE
        else:
            style = CONTEXT
        ptr = "›" if current else " "
        out.append(Text(f" {ptr} {cat.name}".ljust(sidebar_w), style=style))
    return out


def _field_line(fld, config, width, *, active: bool) -> Text:
    label, value = fld.label, str(fld.summary(config))
    if active:
        return Text(f" › {label:<{_LABEL_W}}{value}", style=HILITE)  # padded by caller
    row = Text(f"   {label:<{_LABEL_W}}", style=MUTED)
    row.append(value, style=MOKO_PINK_DIM)
    return row

