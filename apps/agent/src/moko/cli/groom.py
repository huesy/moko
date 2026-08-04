"""`moko groom` — outward maintenance, the counterpart to dreaming.

Dreaming defragments the *mind* (memory, thoughts, knowledge). Grooming tends
the *outside*: the Claude subscription token, the `claude` binary, Moko's own
version, the background service. Safe upkeep (refreshing a near-expiry token) is
done automatically; upgrades that change code or system tools are reported with
the command to run — never applied silently, since that crosses the consent
line. `moko groom --upgrade` opts in to the code update.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from rich.panel import Panel
from rich.table import Table

import moko

from ..clock import now_utc
from ..config import config as cfg
from ..theme import console
from . import service

_OK, _INFO, _WARN = "ok", "info", "warn"
_ICON = {_OK: "[moko]✓[/moko]", _INFO: "[muted]•[/muted]", _WARN: "[yellow]●[/yellow]"}
_TOKEN_REFRESH_BELOW_H = 2.0  # proactively refresh a subscription token under this


def _repo_root() -> Path:
    # Walk up from the installed package to the nearest git checkout. The
    # package sits at <repo>/apps/agent/src/moko/__init__.py, but don't bank on
    # the depth — find .git so the layout can move without breaking grooming.
    start = Path(moko.__file__).resolve()
    for parent in start.parents:
        if (parent / ".git").exists():
            return parent
    return start.parents[2]  # fallback: the agent app dir


def _token_check(config: cfg.Config) -> tuple[str, str, str]:
    if config.llm_auth_method != "claude_code":
        return ("Subscription", _INFO, "not using Claude Code sign-in")
    from ..llm import claude_oauth

    creds = claude_oauth.read_credentials()
    if not creds:
        return ("Subscription", _WARN, "not signed in — run [accent]claude[/accent] and log in")
    exp = creds.get("expiresAt")
    if not exp:
        return ("Subscription", _OK, "signed in")
    hours = (exp / 1000 - now_utc().timestamp()) / 3600
    if hours < _TOKEN_REFRESH_BELOW_H:  # safe auto-upkeep: refresh it now
        try:
            claude_oauth.refresh(creds["refreshToken"])
            return ("Subscription", _OK, "token refreshed")
        except Exception as exc:  # noqa: BLE001 — surface the refresh failure
            return ("Subscription", _WARN, f"token near expiry; refresh failed ({exc})")
    return ("Subscription", _OK, f"token valid ~{hours:.0f}h")


def _claude_cli_check() -> tuple[str, str, str]:
    path = shutil.which("claude")
    if not path:
        return ("Claude CLI", _INFO, "not installed (only needed for Claude Code sign-in)")
    from ..llm import claude_oauth

    return ("Claude CLI", _OK, f"v{claude_oauth.claude_code_version()} — keep it current")


def _git_behind(root: Path) -> int | None:
    """How many commits the local checkout is behind its upstream, or None if
    this isn't a git checkout / has no upstream."""
    if not (root / ".git").exists():
        return None
    try:
        subprocess.run(["git", "fetch", "--quiet"], cwd=root, timeout=20, check=False)
        r = subprocess.run(
            ["git", "rev-list", "--count", "HEAD..@{u}"],
            cwd=root, capture_output=True, text=True, timeout=10, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return int(r.stdout.strip()) if r.returncode == 0 and r.stdout.strip().isdigit() else None


def _moko_check(root: Path) -> tuple[str, str, str]:
    behind = _git_behind(root)
    if behind:
        return ("Moko", _WARN, f"v{moko.__version__} · {behind} commit(s) behind — moko update")
    if not (root / ".git").exists():
        # A release-wheel install (no git) — compare against the latest release.
        from . import update

        rel = update.latest_release()
        if rel and update.is_newer(rel[0], moko.__version__):
            return ("Moko", _WARN, f"v{moko.__version__} · {rel[0]} available — moko update")
    return ("Moko", _OK, f"v{moko.__version__} · up to date")


def _service_check() -> tuple[str, str, str]:
    s = service.service_state()
    if not s.get("available"):
        return ("Service", _INFO, "not managed by systemd here")
    if s.get("active"):
        return ("Service", _OK, "running")
    return ("Service", _WARN, "stopped — [accent]moko service start[/accent]")


def run(upgrade: bool = False) -> int:
    config = cfg.load()
    if config is None:
        console.print("We haven't met yet — run [accent]moko setup[/accent] first. 🌸")
        return 0
    root = _repo_root()
    rows = [_token_check(config), _claude_cli_check(), _moko_check(root), _service_check()]
    table = Table(show_header=False, box=None, padding=(0, 2, 0, 0))
    table.add_column(justify="center")
    table.add_column(style="muted", justify="right")
    table.add_column()
    for name, status, detail in rows:
        table.add_row(_ICON[status], name, detail)
    console.print(
        Panel(table, border_style="moko", title="[accent]moko groom 🌸[/accent]", padding=(1, 2))
    )

    if upgrade:
        from . import update

        return update.run()
    if any(name == "Moko" and status == _WARN for name, status, _ in rows):
        console.print(
            "[muted]An update's available — apply with [accent]moko update[/accent].[/muted]"
        )
    return 0
