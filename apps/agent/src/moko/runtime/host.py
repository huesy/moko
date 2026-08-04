"""The host runtime — Moko as a ``systemd --user`` service.

This is the behaviour that used to live in ``cli/service.py``, unchanged for a
plain install: write the unit, enable it with **linger** (so Moko survives
logout and starts on reboot), and proxy start / stop / restart / status / logs.
State still lives in ``~/.moko``; this just keeps ``moko start`` running
unattended. When ``runtime.hardened`` is on/strict, the unit gains a sandbox
profile (see `hardening.py`) — otherwise it is byte-for-byte the original unit.
"""

from __future__ import annotations

import contextlib
import getpass
import shutil
import subprocess
import sys
import time
from pathlib import Path

from ..config.paths import moko_home
from ..theme import console
from .base import Runtime
from .hardening import hardening_block

UNIT_NAME = "moko.service"
UNIT_PATH = Path.home() / ".config" / "systemd" / "user" / UNIT_NAME


def _moko_bin() -> str:
    """The `moko` entry point to run — prefer the one beside this interpreter
    (the venv we're running in), so the service uses the same environment."""
    beside = Path(sys.executable).parent / "moko"
    return str(beside) if beside.exists() else (shutil.which("moko") or "moko")


def _service_path() -> str:
    """The PATH the daemon runs with. A `systemd --user` unit inherits a bare
    PATH that omits `~/.local/bin`, so a subprocess Moko needs — notably the
    `claude` binary for Claude Code sign-in — wouldn't be found. Prepend the
    venv, the discovered `claude`'s directory, and `~/.local/bin`."""
    parts = [str(Path(sys.executable).parent)]  # venv bin (our own tools)
    claude = shutil.which("claude")
    if claude:
        parts.append(str(Path(claude).parent))
    parts.append(str(Path.home() / ".local" / "bin"))
    parts += ["/usr/local/bin", "/usr/bin", "/bin", "/snap/bin"]
    seen: set[str] = set()
    return ":".join(p for p in parts if p and not (p in seen or seen.add(p)))


def _hardening(config) -> str:
    """The hardening block for the unit, from config (empty when off / no config)."""
    if config is None:
        return ""
    rt = getattr(config, "runtime", None)
    level = getattr(rt, "hardened", "off") if rt else "off"
    if level not in ("on", "strict"):
        return ""
    rw = [str(moko_home())] + list(getattr(rt, "mounts", []) or [])
    return hardening_block(level, rw)


def _unit_text(config=None) -> str:
    """The systemd unit. With no config (or hardened=off) this is the original
    plain unit; with hardening on/strict a sandbox block is spliced into [Service]."""
    hardening = _hardening(config)
    hardening_lines = f"{hardening}" if hardening else ""
    return f"""\
[Unit]
Description=Moko — your always-on companion 🌸
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory={Path.home()}
Environment=PATH={_service_path()}
ExecStart={_moko_bin()} start
Restart=on-failure
RestartSec=5
# Moko shuts down gracefully on SIGTERM in well under this; the cap just means a
# rare hung shutdown costs seconds, not systemd's 90s default, before SIGKILL.
TimeoutStopSec=10
KillMode=mixed
{hardening_lines}
[Install]
WantedBy=default.target
"""


def _systemctl(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["systemctl", "--user", *args], text=True, capture_output=True, check=False
    )


class HostRuntime(Runtime):
    def __init__(self, config=None):
        self.config = config

    # ---- guards / helpers ----------------------------------------------------

    def _require_systemd(self) -> bool:
        if shutil.which("systemctl") is None:
            console.print(
                "[moko]systemd isn't available here.[/moko] On this machine, run "
                "[accent]moko start[/accent] yourself (e.g. under tmux), or install the unit "
                f"by hand from [accent]{UNIT_PATH}[/accent]."
            )
            return False
        probe = _systemctl("--version")
        if probe.returncode != 0:
            console.print(f"[moko]Can't reach your user systemd:[/moko] {probe.stderr.strip()}")
            return False
        return True

    def _enable_linger(self) -> None:
        """Let the service run without an active login and survive reboot."""
        if shutil.which("loginctl") is None:
            return
        user = getpass.getuser()
        r = subprocess.run(
            ["loginctl", "enable-linger", user], text=True, capture_output=True, check=False
        )
        if r.returncode == 0:
            console.print("Enabled linger — Moko will start on boot, no login needed.")
        else:
            console.print(
                "[dim]Couldn't enable linger automatically. For start-on-boot, run:[/dim]\n"
                f"  [accent]sudo loginctl enable-linger {user}[/accent]"
            )

    def _proxy(self, action: str) -> int:
        if not self._require_systemd():
            return 1
        r = _systemctl(action, UNIT_NAME)
        if r.returncode != 0:
            console.print(f"[moko]Couldn't {action} Moko:[/moko] {(r.stderr or 'failed').strip()}")
            return r.returncode
        # Confirm what actually happened — `systemctl start/stop` say nothing on
        # success, and a daemon can start then crash-loop, so check the real state.
        if action in ("start", "restart"):
            time.sleep(0.8)  # a beat, so an immediate crash shows up here
        s = self.state()
        if action == "stop":
            console.print("[moko]Moko has stopped.[/moko] 🌸")
            return 0
        if s["active"]:
            boot = "  It'll start on boot, too." if s["enabled"] else ""
            console.print(f"[moko]Moko is running.[/moko] 🌸{boot}")
            return 0
        console.print(
            f"[moko]{action.capitalize()} ran, but Moko isn't staying up.[/moko] "
            "Check [accent]moko doctor[/accent] or [accent]moko service logs[/accent]."
        )
        return 1

    # ---- Runtime interface ---------------------------------------------------

    def install(self) -> int:
        if not self._require_systemd():
            return 1
        UNIT_PATH.parent.mkdir(parents=True, exist_ok=True)
        UNIT_PATH.write_text(_unit_text(self.config), encoding="utf-8")
        console.print(f"Wrote [accent]{UNIT_PATH}[/accent]")
        _systemctl("daemon-reload")
        enabled = _systemctl("enable", "--now", UNIT_NAME)
        if enabled.returncode != 0:
            console.print(f"[moko]Couldn't start the service:[/moko]\n{enabled.stderr.strip()}")
            return 1
        self._enable_linger()
        console.print(
            "[moko]Moko is running in the background.[/moko] 🌸  "
            "Check it with [accent]moko service status[/accent] or "
            "[accent]moko service logs[/accent]."
        )
        return 0

    def uninstall(self) -> int:
        if not self._require_systemd():
            return 1
        _systemctl("disable", "--now", UNIT_NAME)
        if UNIT_PATH.exists():
            UNIT_PATH.unlink()
        _systemctl("daemon-reload")
        console.print(
            "Moko's background service is removed. (Linger left as-is; disable with "
            f"[accent]sudo loginctl disable-linger {getpass.getuser()}[/accent] if you want.)"
        )
        return 0

    def start(self) -> int:
        return self._proxy("start")

    def stop(self) -> int:
        return self._proxy("stop")

    def restart(self) -> int:
        return self._proxy("restart")

    def status(self) -> int:
        if not self._require_systemd():
            return 1
        # status returns non-zero when inactive; show the output regardless.
        r = _systemctl("status", UNIT_NAME, "--no-pager")
        console.print((r.stdout or r.stderr).strip())
        return 0

    def logs(self) -> int:
        if shutil.which("journalctl") is None:
            return self.status()
        console.print("[dim]Following Moko's logs — Ctrl+C to stop.[/dim]")
        with contextlib.suppress(KeyboardInterrupt):
            # Stream straight to the terminal (no capture); blocks until Ctrl+C.
            subprocess.run(
                ["journalctl", "--user", "-u", UNIT_NAME, "-n", "100", "-f"], check=False
            )
        return 0

    def state(self) -> dict:
        if shutil.which("systemctl") is None:
            return {"available": False, "installed": False, "active": False, "enabled": False}
        return {
            "available": True,
            "installed": UNIT_PATH.exists(),
            "active": _systemctl("is-active", UNIT_NAME).stdout.strip() == "active",
            "enabled": _systemctl("is-enabled", UNIT_NAME).stdout.strip() == "enabled",
        }

    def status_line(self) -> str:
        s = self.state()
        if not s["available"]:
            return "[muted]not managed here — run [accent]moko start[/accent] yourself[/muted]"
        if not s["installed"]:
            return "[muted]not installed — [accent]moko service install[/accent][/muted]"
        if s["active"]:
            return "[moko]running[/moko]" + (" · starts on boot" if s["enabled"] else "")
        return "[muted]installed but stopped — [accent]moko service start[/accent][/muted]"

    def restart_quiet(self) -> bool:
        if shutil.which("systemctl") is None:
            return False
        if _systemctl("restart", UNIT_NAME).returncode != 0:
            return False
        time.sleep(0.8)  # a beat, so an immediate crash-on-start shows as inactive
        return self.state().get("active", False)
