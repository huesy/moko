"""`moko doctor` — a quick health scan: what's set up, running, or needs a nudge.

Local checks only (fast, no network surprises): config, the brain's
credentials, Telegram pairing, the background service, and on-disk memory.
Each line is ✓ good · ● optional/unset · ✗ needs fixing.
"""

from __future__ import annotations

import asyncio
import os
import shutil

from rich.panel import Panel
from rich.table import Table

from ..config import config as cfg
from ..config import secrets
from ..config.paths import db_path, skills_dir
from ..config.providers import ENV_VARS
from ..skills import loader
from ..store import Store, integrity
from ..theme import console
from . import service

_OK, _WARN, _BAD = "ok", "warn", "bad"
_ICON = {_OK: "[moko]✓[/moko]", _WARN: "[yellow]●[/yellow]", _BAD: "[red]✗[/red]"}


def _brain_check(config: cfg.Config) -> tuple[str, str, str]:
    provider = config.llm_provider
    if provider in ("", "none"):
        return ("Brain", _WARN, "no AI chosen yet — run [accent]moko setup[/accent]")
    base = f"{provider} · {config.llm_model}" if config.llm_model else provider
    auth = config.llm_auth_method
    if auth == "claude_code":
        from ..llm import claude_oauth

        if claude_oauth.read_credentials() is None:
            hint = "not signed in — run [accent]claude[/accent] and log in"
            if shutil.which("claude") is None:
                hint = "`claude` not on PATH"
            return ("Brain", _BAD, f"{base} · Claude Code — {hint}")
        return ("Brain", _OK, f"{base} · Claude Code (subscription)")
    if auth == "env":
        var = ENV_VARS.get(provider, "")
        ok = bool(var and os.environ.get(var))
        detail = f"{base} · env {var or '?'}" + ("" if ok else " not set")
        return ("Brain", _OK if ok else _BAD, detail)
    if auth == "oauth":
        ok = bool(secrets.get_oauth_tokens(provider))
        tail = "OAuth connected" if ok else "not connected — [accent]moko login[/accent]"
        return ("Brain", _OK if ok else _BAD, f"{base} · {tail}")
    ok = bool(secrets.get_api_key(provider))
    tail = "API key stored" if ok else "no API key — [accent]moko setup[/accent]"
    return ("Brain", _OK if ok else _BAD, f"{base} · {tail}")


def _telegram_check(config: cfg.Config) -> tuple[str, str, str]:
    paired = bool(config.telegram_user_id)
    has_token = bool(secrets.get_secret("telegram_bot_token"))
    if paired and has_token:
        return ("Telegram", _OK, f"paired (id {config.telegram_user_id})")
    if has_token:
        return ("Telegram", _WARN, "bot token set, not paired — [accent]moko pair[/accent]")
    return ("Telegram", _WARN, "not connected — pair, or use [accent]moko chat[/accent]")


def _runtime_check(config: cfg.Config) -> tuple[str, str, str]:
    """Which tier Moko runs in (SANDBOX.md) — host / hardened host / container."""
    rt = config.runtime
    if rt.is_container:
        return ("Runtime", _OK, f"{rt.mode} · image {rt.image}")
    if rt.hardened in ("on", "strict"):
        return ("Runtime", _OK, f"host · hardened ({rt.hardened})")
    return ("Runtime", _OK,
            "host · plain — harden with [accent]moko config runtime.hardened on[/accent]")


def _service_check(config: cfg.Config | None = None) -> tuple[str, str, str]:
    s = service.service_state()
    container = bool(config and config.runtime.is_container)
    if not s["available"]:
        if container:
            return ("Service", _WARN,
                    f"{config.runtime.mode} not available — install it or switch tiers")
        return ("Service", _WARN, "no systemd here — run [accent]moko start[/accent] yourself")
    if not s["installed"]:
        return ("Service", _WARN, "not installed — [accent]moko service install[/accent]")
    if s["active"]:
        return ("Service", _OK, "running" + (" · starts on boot" if s["enabled"] else ""))
    return ("Service", _BAD, "installed but stopped — [accent]moko service start[/accent]")


def _memory_check() -> tuple[str, str, str]:
    db = db_path()
    if db.exists():
        return ("Memory", _OK, f"{db} ({db.stat().st_size // 1024} KB)")
    return ("Memory", _WARN, "empty — it'll be created on first run")


def _skills_check() -> tuple[str, str, str]:
    """Any user skill that overrides a core built-in by name. Overriding is a
    supported power (author_skill blocks it, but a hand-placed file can still do
    it) — surface it so a shadowed safety-floor skill is never silent."""
    builtin = set(loader._discover(loader._BUILTIN))
    user = set(loader._discover(skills_dir()))
    shadowed = sorted(builtin & user)
    if not shadowed:
        return ("Skills", _OK, "no user skill overrides a core ability")
    return ("Skills", _WARN,
            f"user skill(s) override a core ability: {', '.join(shadowed)} "
            f"— remove from {skills_dir()} to restore the built-in")


def checks(config: cfg.Config) -> list[tuple[str, str, str]]:
    setup_detail = f"configured for [accent]{config.companion_name}[/accent] · {config.timezone}"
    return [
        ("Setup", _OK, setup_detail),
        _brain_check(config),
        _telegram_check(config),
        _runtime_check(config),
        _service_check(config),
        _memory_check(),
        _skills_check(),
    ]


def _integrity_findings() -> list[integrity.Finding] | None:
    """Run the deep mind scan against a read-only view of the graph. None when
    there's no DB yet (nothing to check). Safe while the daemon runs — WAL lets a
    second reader in, and the scan never writes."""
    db = db_path()
    if not db.exists():
        return None

    async def _run() -> list[integrity.Finding]:
        store = await Store.open(db)
        try:
            return await integrity.check(store.conn)
        finally:
            await store.close()

    try:
        return asyncio.run(_run())
    except Exception as exc:  # noqa: BLE001 — a scan that can't open the DB IS the finding
        return [integrity.Finding("Mind", "error", 1, f"couldn't read the graph: {exc}")]


def _integrity_icon(f: integrity.Finding) -> str:
    if f.ok:
        return _ICON[_OK]
    return _ICON[_BAD] if f.severity == "error" else _ICON[_WARN]


def run() -> int:
    config = cfg.load()
    if config is None:
        console.print("We haven't met yet — run [accent]moko setup[/accent] first. 🌸")
        return 0
    rows = checks(config)
    table = Table(show_header=False, box=None, padding=(0, 2, 0, 0))
    table.add_column(justify="center")
    table.add_column(style="muted", justify="right")
    table.add_column()
    for name, status, detail in rows:
        table.add_row(_ICON[status], name, detail)
    console.print(
        Panel(table, border_style="moko", title="[accent]moko doctor 🌸[/accent]", padding=(1, 2))
    )
    bad = sum(1 for _, status, _ in rows if status == _BAD)
    warn = sum(1 for _, status, _ in rows if status == _WARN)

    # The deep scan: is the mind itself sound? (provenance, supersede, recall).
    findings = _integrity_findings()
    if findings is not None:
        itable = Table(show_header=False, box=None, padding=(0, 2, 0, 0))
        itable.add_column(justify="center")
        itable.add_column(style="muted", justify="right")
        itable.add_column()
        for f in findings:
            itable.add_row(_integrity_icon(f), f.name, f.detail)
        console.print(
            Panel(itable, border_style="moko",
                  title="[accent]mind integrity 🧠[/accent]", padding=(1, 2))
        )
        problems = [f for f in findings if not f.ok]
        for f in problems:
            if f.hint:
                console.print(f"  [muted]{f.name}: {f.hint}[/muted]")
        bad += sum(1 for f in problems if f.severity == "error")
        warn += sum(1 for f in problems if f.severity == "warn")

    if bad:
        console.print(f"[red]{bad} thing(s) need fixing before Moko runs right.[/red]")
    elif warn:
        console.print(f"[yellow]{warn} thing(s) worth a look — Moko can still run.[/yellow]")
    else:
        console.print("[moko]All good — set up right, and the mind is sound. 🌸[/moko]")

    # Honest about what a sandbox does (and doesn't): it bounds host blast radius,
    # not exfiltration. Only shown when a containment tier is actually on.
    rt = config.runtime
    if rt.is_container or rt.hardened in ("on", "strict"):
        console.print(
            "[muted]Containment bounds host filesystem/process damage — it doesn't seal "
            "network egress or hide secrets from Moko. The consent gate stays the exfil "
            "defense.[/muted]"
        )
    return 1 if bad else 0
