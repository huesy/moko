"""`moko update` — bring Moko up to the latest release.

Two install shapes, one command:
  - a **release-wheel** install (the normal path via install.sh) updates by fetching
    the newest GitHub Release wheel — which carries the built dashboard — and
    re-installing it with uv;
  - a **dev / editable git checkout** updates by pulling + re-syncing.
Either way the background service is restarted so the new code takes over. `--check`
only reports whether an update is available, changing nothing.

Fetching the release uses the public GitHub API; `gh` is tried first when
installed (higher rate limits / authenticated forks). This mirrors install.sh,
so install and update resolve the same "latest release".
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

import moko

from ..theme import console
from . import service

# owner/repo; overridable for a fork, matching install.sh.
MOKO_SLUG = os.environ.get("MOKO_SLUG", "huesy/moko")
_PYTHON = os.environ.get("MOKO_PYTHON", "3.12")


def _git_checkout_root() -> Path | None:
    """The repo root if Moko runs from a git checkout (dev / editable install), else
    None (a wheel install has no .git — it updates from a release)."""
    for parent in Path(moko.__file__).resolve().parents:
        if (parent / ".git").exists():
            return parent
    return None


def _release_json() -> dict | None:
    """The latest release's API object — via `gh` when available, else the public
    GitHub API. None when it can't be determined (no network / no releases)."""
    if shutil.which("gh"):
        r = subprocess.run(
            ["gh", "api", f"repos/{MOKO_SLUG}/releases/latest"],
            capture_output=True, text=True, check=False,
        )
        if r.returncode == 0:
            try:
                return json.loads(r.stdout)
            except json.JSONDecodeError:
                pass
    try:
        import httpx

        resp = httpx.get(
            f"https://api.github.com/repos/{MOKO_SLUG}/releases/latest",
            timeout=15, follow_redirects=True,
        )
        if resp.status_code == 200:
            return resp.json()
    except Exception:  # noqa: BLE001 — network/parse issues just mean "can't tell"
        pass
    return None


def latest_release() -> tuple[str, str] | None:
    """(tag, wheel_download_url) for the latest release, or None if unavailable."""
    data = _release_json()
    if not data:
        return None
    tag = data.get("tag_name")
    wheel = next(
        (a.get("browser_download_url") for a in data.get("assets", [])
         if str(a.get("name", "")).endswith(".whl")),
        None,
    )
    return (tag, wheel) if tag and wheel else None


def is_newer(tag: str, current: str) -> bool:
    """Whether release `tag` (e.g. 'v0.1.0') is newer than the installed `current`."""
    candidate = tag.lstrip("vV")
    try:
        from packaging.version import InvalidVersion, Version
    except ImportError:
        # packaging is declared in pyproject; keep a fallback for stripped envs.
        return candidate != current
    try:
        return Version(candidate) > Version(current)
    except InvalidVersion:
        return candidate != current  # unparseable (odd dev build) → update if different


def _restart_service() -> None:
    if service.service_state().get("installed"):
        service.run("restart")


def _update_from_git(root: Path) -> int:
    console.print("[moko]Updating Moko from your git checkout…[/moko]")
    agent = Path(moko.__file__).resolve().parents[2]  # <repo>/apps/agent
    steps = ((["git", "pull", "--ff-only"], root), (["uv", "sync", "--extra", "dev"], agent))
    for cmd, cwd in steps:
        if subprocess.run(cmd, cwd=cwd, check=False).returncode != 0:
            console.print(f"[moko]`{' '.join(cmd)}` failed — stopping.[/moko]")
            return 1
    _restart_service()
    console.print("[moko]Updated. 🌸[/moko]")
    return 0


def _download_wheel(tag: str, wheel_url: str | None) -> str | None:
    """Fetch the release wheel to a temp dir; return its path (`gh release download`
    when available, else a public HTTPS download). None on failure."""
    tmp = tempfile.mkdtemp(prefix="moko-update-")
    if shutil.which("gh"):
        r = subprocess.run(
            ["gh", "release", "download", tag, "--repo", MOKO_SLUG, "--pattern", "*.whl",
             "--dir", tmp],
            capture_output=True, text=True, check=False,
        )
        if r.returncode == 0:
            found = next((str(p) for p in Path(tmp).glob("*.whl")), None)
            if found:
                return found
    if wheel_url:
        try:
            import httpx

            # uv requires a PEP 427 wheel filename (…-py3-none-any.whl), not a bare
            # "moko.whl" — same footgun as install.sh's HTTPS download path.
            name = Path(wheel_url.split("?", 1)[0]).name
            if not name.endswith(".whl"):
                name = "moko-0-py3-none-any.whl"
            dest = Path(tmp) / name
            with httpx.stream("GET", wheel_url, timeout=60, follow_redirects=True) as resp:
                if resp.status_code == 200:
                    with dest.open("wb") as fh:
                        for chunk in resp.iter_bytes():
                            fh.write(chunk)
                    return str(dest)
        except Exception:  # noqa: BLE001
            pass
    return None


def _update_from_release(*, check_only: bool) -> int:
    rel = latest_release()
    if rel is None:
        console.print(
            "[moko]Couldn't check for updates.[/moko] Check your network, or that "
            f"[accent]https://github.com/{MOKO_SLUG}/releases[/accent] has a release."
        )
        return 1
    tag, wheel_url = rel
    current = moko.__version__
    if not is_newer(tag, current):
        console.print(f"[moko]Moko is up to date[/moko] (v{current}).")
        return 0
    console.print(f"[moko]Update available:[/moko] {tag} [muted](you have v{current})[/muted]")
    if check_only:
        console.print("[muted]Install it with [accent]moko update[/accent].[/muted]")
        return 0
    console.print("[accent]Downloading and installing…[/accent] 🌸")
    wheel = _download_wheel(tag, wheel_url)
    if wheel is None:
        console.print(
            "[moko]Couldn't download the release wheel.[/moko] "
            "Check your network and try again."
        )
        return 1
    ok = subprocess.run(
        ["uv", "tool", "install", "--force", "--python", _PYTHON, wheel], check=False
    ).returncode == 0
    if not ok:
        console.print("[moko]The install step failed.[/moko]")
        return 1
    _restart_service()
    console.print(f"[moko]Updated to {tag}. 🌸[/moko]")
    return 0


def run(*, check_only: bool = False) -> int:
    root = _git_checkout_root()
    if root is not None:
        if check_only:
            console.print(
                "[muted]This is a dev checkout — run [accent]moko update[/accent] to pull "
                "and re-sync.[/muted]"
            )
            return 0
        return _update_from_git(root)
    return _update_from_release(check_only=check_only)
