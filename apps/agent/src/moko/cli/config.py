"""`moko config` — view and change Moko's settings, anytime.

Two ways in:

  * **Interactive** — `moko config` with no arguments opens the full-screen,
    keyboard-driven editor (see :mod:`moko.cli.config_tui`). Everything is edited
    in-frame, including the AI brain and dashboard access.
  * **Scriptable** — `moko config KEY [VALUE]` gets or sets one setting from the
    command line, for dotfiles, SSH one-liners, and provisioning (the TUI needs a
    real terminal; this doesn't). `moko config --list` dumps everything.

Keys are the dotted paths from ``~/.moko/config.yaml`` (e.g. ``companion.name``,
``cadence.outreach_settle_minutes``). The *coupled, side-effecting* settings —
the brain (``llm.*``) and dashboard ``access.monitor`` — are read-only here and
must be set in the interactive editor, which manages their model/auth pairing,
token, firewall, and restart as a unit.
"""

from __future__ import annotations

import re
import sys
from collections.abc import Callable
from dataclasses import dataclass

from rich.markup import escape

from .. import i18n
from ..config import config as cfg
from ..theme import console
from . import catalog, config_tui

_PROACTIVITY = {value for value, _ in catalog.PROACTIVITY}
_INTERESTS = {value for value, _ in catalog.INTERESTS}
_VERBOSITY = set(cfg.CHAT_VERBOSITY)
_LANGUAGES = {code for code, _, _ in i18n.LANGUAGES}
_BRAIN_NOTE = "set the brain in `moko config` (Brain) or `moko setup` — it pairs model + sign-in"
_ACCESS_NOTE = "set in `moko config` (Access) — it manages the token, firewall, and restart"


@dataclass
class _Spec:
    get: Callable[[cfg.Config], str]  # current value as a display string
    set: Callable[[cfg.Config, str], None] | None = None  # None → read-only here
    note: str = ""  # why it's read-only (shown on a blocked set)


# ── value parsers (raise ValueError with a friendly message on bad input) ─────


def _str_attr(attr: str, label: str) -> Callable:
    def setter(c: cfg.Config, v: str) -> None:
        v = v.strip()
        if not v:
            raise ValueError(f"{label} can't be empty")
        setattr(c, attr, v)
    return setter


def _enum_attr(attr: str, allowed: set[str], label: str) -> Callable:
    def setter(c: cfg.Config, v: str) -> None:
        v = v.strip().lower()
        if v not in allowed:
            raise ValueError(f"{label} must be one of: {', '.join(sorted(allowed))}")
        setattr(c, attr, v)
    return setter


def _time_attr(attr: str) -> Callable:
    def setter(c: cfg.Config, v: str) -> None:
        v = v.strip()
        if not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", v):
            raise ValueError("time must be HH:MM, 24-hour (e.g. 23:00)")
        setattr(c, attr, v)
    return setter


def _posint_cadence(attr: str) -> Callable:
    def setter(c: cfg.Config, v: str) -> None:
        try:
            n = int(v.strip())
        except ValueError:
            raise ValueError(f"{attr} must be a whole number of minutes") from None
        if n <= 0:
            raise ValueError(f"{attr} must be greater than 0")
        setattr(c.cadence, attr, n)
    return setter


def _bool_attr(attr: str, label: str) -> Callable:
    _TRUE = {"on", "yes", "true", "1", "enable", "enabled"}
    _FALSE = {"off", "no", "false", "0", "disable", "disabled"}

    def setter(c: cfg.Config, v: str) -> None:
        s = v.strip().lower()
        if s in _TRUE:
            setattr(c, attr, True)
        elif s in _FALSE:
            setattr(c, attr, False)
        else:
            raise ValueError(f"{label} must be on or off (yes/no)")
    return setter


def _set_timezone(c: cfg.Config, v: str) -> None:
    from zoneinfo import available_timezones
    v = v.strip()
    if v not in available_timezones():
        raise ValueError(f"unknown timezone: {v!r} — use an IANA name like Australia/Brisbane")
    c.timezone = v


def _set_interests(c: cfg.Config, v: str) -> None:
    items = [x.strip() for x in v.split(",") if x.strip()]
    bad = [x for x in items if x not in _INTERESTS]
    if bad:
        raise ValueError(
            f"unknown interests: {', '.join(bad)} — valid: {', '.join(sorted(_INTERESTS))}"
        )
    c.interests = items


_MODEL_PROVIDERS = {
    "none", "anthropic", "openai", "xai", "google", "openai_compatible", "elevenlabs", "local",
}
_MODEL_AUTHS = {"", "api_key", "claude_code", "oauth", "env", "local"}


def _model_field(modality: str, field: str, allowed: set[str] | None = None) -> Callable:
    """Set one field of a per-modality model (vision/image/speech/hearing). The API
    key isn't set here — store it with the guided setup or `moko secret` — so this
    only records provider/model/auth. A restart applies it (the client is built at
    startup)."""
    def setter(c: cfg.Config, v: str) -> None:
        v = v.strip()
        if allowed is not None and v.lower() not in allowed:
            opts = ", ".join(sorted(a for a in allowed if a)) or "(none)"
            raise ValueError(f"{field} must be one of: {opts}")
        choice = c.models.get(modality) or cfg.ModelChoice()
        setattr(choice, field, v.lower() if allowed else v)
        c.models[modality] = choice
    return setter


# The settable surface, keyed by the dotted config path. Order = `--list` order.
_SPECS: dict[str, _Spec] = {
    "companion.name": _Spec(lambda c: c.companion_name, _str_attr("companion_name", "name")),
    "language": _Spec(lambda c: c.language, _enum_attr("language", _LANGUAGES, "language")),
    "timezone": _Spec(lambda c: c.timezone, _set_timezone),
    "sleep.start": _Spec(lambda c: c.sleep_start, _time_attr("sleep_start")),
    "sleep.end": _Spec(lambda c: c.sleep_end, _time_attr("sleep_end")),
    "proactivity": _Spec(lambda c: c.proactivity,
                         _enum_attr("proactivity", _PROACTIVITY, "proactivity")),
    "chat.verbosity": _Spec(lambda c: c.chat_verbosity,
                            _enum_attr("chat_verbosity", _VERBOSITY, "verbosity")),
    "interests": _Spec(lambda c: ",".join(c.interests), _set_interests),
    "cadence.outreach_settle_minutes": _Spec(
        lambda c: str(c.cadence.outreach_settle_minutes),
        _posint_cadence("outreach_settle_minutes")),
    "cadence.reflect_muse_interval_minutes": _Spec(
        lambda c: str(c.cadence.reflect_muse_interval_minutes),
        _posint_cadence("reflect_muse_interval_minutes")),
    "curiosity.autonomous_research": _Spec(
        lambda c: "on" if c.allow_autonomous_research else "off",
        _bool_attr("allow_autonomous_research", "autonomous research")),
    # Coupled / side-effecting — readable here, set in the interactive editor.
    "llm.provider": _Spec(lambda c: c.llm_provider, None, _BRAIN_NOTE),
    "llm.model": _Spec(lambda c: c.llm_model, None, _BRAIN_NOTE),
    "llm.auth_method": _Spec(lambda c: c.llm_auth_method, None, _BRAIN_NOTE),
    "llm.base_url": _Spec(lambda c: c.llm_base_url, None, _BRAIN_NOTE),
    "access.monitor": _Spec(lambda c: c.monitor_access, None, _ACCESS_NOTE),
}

# Per-modality models (vision/image/speech/hearing): provider/model/auth settable
# here; the API key is captured by the guided setup (or `moko secret`). Restart to
# apply. Built from cfg.MODALITIES so the keys can't drift from the schema.
for _cap in cfg.MODALITIES:
    _SPECS[f"models.{_cap}.provider"] = _Spec(
        (lambda c, k=_cap: c.model_for(k).provider),
        _model_field(_cap, "provider", _MODEL_PROVIDERS),
    )
    _SPECS[f"models.{_cap}.model"] = _Spec(
        (lambda c, k=_cap: c.model_for(k).model or "—"),
        _model_field(_cap, "model"),
    )
    _SPECS[f"models.{_cap}.auth"] = _Spec(
        (lambda c, k=_cap: c.model_for(k).auth or "—"),
        _model_field(_cap, "auth", _MODEL_AUTHS),
    )


def _raw_attr(attr: str) -> Callable:
    """A plain string setter that allows clearing (empty) — for free-text/path config."""
    def setter(c: cfg.Config, v: str) -> None:
        setattr(c, attr, v.strip())
    return setter


# Editable in the TUI too, but kept on the CLI surface for parity (so `moko config KEY`
# can read/set every option). The API key stays a secret (`moko secret`), never here.
_SPECS["location"] = _Spec(
    lambda c: c.location_name or "—", _raw_attr("location_name"))
_SPECS["persona.self_portrait_reference"] = _Spec(
    lambda c: c.self_portrait_reference or "—", _raw_attr("self_portrait_reference"))
_SPECS["persona.image_character"] = _Spec(
    lambda c: c.image_character or "—", _raw_attr("image_character"))
_SPECS["telegram.user_id"] = _Spec(
    lambda c: c.telegram_user_id or "—", _raw_attr("telegram_user_id"))
_SPECS["telegram.chat_id"] = _Spec(
    lambda c: c.telegram_chat_id or "—", _raw_attr("telegram_chat_id"))


# Runtime tier (SANDBOX.md): where and how contained Moko runs. Set here or in setup;
# a change needs a restart to apply (the runtime is chosen at start).
def _runtime_enum(attr: str, allowed: tuple[str, ...], label: str) -> Callable:
    def setter(c: cfg.Config, v: str) -> None:
        v = v.strip().lower()
        if v not in allowed:
            raise ValueError(f"{label} must be one of: {', '.join(allowed)}")
        setattr(c.runtime, attr, v)
    return setter


def _runtime_raw(attr: str) -> Callable:
    def setter(c: cfg.Config, v: str) -> None:
        setattr(c.runtime, attr, v.strip())
    return setter


def _runtime_port(c: cfg.Config, v: str) -> None:
    try:
        n = int(v.strip())
    except ValueError:
        raise ValueError("monitor_port must be a whole number (1–65535)") from None
    if not 1 <= n <= 65535:
        raise ValueError("monitor_port must be between 1 and 65535")
    c.runtime.monitor_port = n


def _runtime_mounts(c: cfg.Config, v: str) -> None:
    # Comma-separated host paths exposed to Moko inside the sandbox (its M7 reach).
    c.runtime.mounts = [x.strip() for x in v.split(",") if x.strip()]


_SPECS["runtime.mode"] = _Spec(
    lambda c: c.runtime.mode, _runtime_enum("mode", cfg.RUNTIME_MODES, "runtime.mode"))
_SPECS["runtime.hardened"] = _Spec(
    lambda c: c.runtime.hardened,
    _runtime_enum("hardened", cfg.HARDENING_LEVELS, "runtime.hardened"))
_SPECS["runtime.image"] = _Spec(lambda c: c.runtime.image, _runtime_raw("image"))
_SPECS["runtime.container_name"] = _Spec(
    lambda c: c.runtime.container_name, _runtime_raw("container_name"))
_SPECS["runtime.monitor_port"] = _Spec(
    lambda c: str(c.runtime.monitor_port), _runtime_port)
_SPECS["runtime.mounts"] = _Spec(
    lambda c: ",".join(c.runtime.mounts) or "—", _runtime_mounts)


def run(args: list[str] | None = None, *, list_all: bool = False, restart: bool = False) -> int:
    config = cfg.load()
    if config is None:
        console.print("We haven't met yet — run [accent]moko setup[/accent] first. 🌸")
        return 0

    args = args or []
    if list_all:
        return _list(config)
    if not args:
        return _interactive(config)
    if len(args) == 1:
        return _get(config, args[0])
    return _set(config, args[0], " ".join(args[1:]), restart=restart)


def _interactive(config: cfg.Config) -> int:
    # The full-screen editor needs a real terminal; fail clearly if piped.
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        console.print(
            "[moko]`moko config` (the editor) needs a terminal.[/moko]\n"
            "[muted]Set one value with [accent]moko config KEY VALUE[/accent], "
            "or list them with [accent]moko config --list[/accent].[/muted]"
        )
        return 1
    config_tui.run(config)
    console.print("[moko]All set. 🌸[/moko]")
    return 0


def _get(config: cfg.Config, key: str) -> int:
    spec = _SPECS.get(key)
    if spec is None:
        return _unknown(key)
    print(spec.get(config))  # bare value on stdout, for scripts ($(moko config timezone))
    return 0


def _set(config: cfg.Config, key: str, value: str, *, restart: bool) -> int:
    spec = _SPECS.get(key)
    if spec is None:
        return _unknown(key)
    if spec.set is None:
        console.print(f"[yellow]{key} is read-only here[/yellow] — {spec.note}.")
        return 1
    try:
        spec.set(config, value)
    except ValueError as exc:
        console.print(f"[yellow]{exc}[/yellow]")
        return 1
    config.save()
    console.print(f"[moko]Saved.[/moko] {key} = [accent]{escape(spec.get(config))}[/accent]")
    _apply_hint(restart=restart)
    return 0


def _list(config: cfg.Config) -> int:
    width = max(len(k) for k in _SPECS)
    for key, spec in _SPECS.items():
        ro = "" if spec.set else "  [muted](read-only — interactive)[/muted]"
        console.print(f"[muted]{key.ljust(width)}[/muted] = {escape(spec.get(config))}{ro}",
                      soft_wrap=True)
    return 0


def _unknown(key: str) -> int:
    settable = ", ".join(k for k, s in _SPECS.items() if s.set)
    console.print(f"[yellow]Unknown setting:[/yellow] {key}")
    console.print(f"[muted]Settable keys:[/muted] {settable}")
    console.print("[muted]See all with [accent]moko config --list[/accent].[/muted]")
    return 1


def _apply_hint(*, restart: bool) -> None:
    """A config change is read at startup, so it needs a restart to go live."""
    from . import service
    try:
        active = service.service_state().get("active")
    except Exception:
        active = False
    if restart:
        if active:
            service.run("restart")  # prints its own confirmation
        else:
            console.print("[muted]Moko isn't running here — it'll apply on next start.[/muted]")
    elif active:
        console.print(
            "[muted]Restart to apply now: [accent]moko service restart[/accent] "
            "(or pass [accent]--restart[/accent]).[/muted]"
        )
