"""The container runtime — Moko inside Docker or Podman (SANDBOX.md).

The container *is* Moko's body: it runs `moko start` as its entrypoint, and every
host-side `moko …` command reaches it through the shared bind mount. The load-
bearing choices:

- **`~/.moko` is bind-mounted at the identical absolute path** (and `MOKO_HOME` is
  set to match), so the control socket + all file-only commands cross the boundary
  unchanged, and the consent gate's workspace path-math stays honest.
- **`~/.moko/claude` → the container's `~/.claude`**, so Claude Code's login lives
  inside the container yet survives image rebuilds (see `moko login`).
- **The monitor binds `0.0.0.0` *inside* the container** and is published only to
  host loopback, preserving "local tier = no auth".
- **Podman is preferred on Linux** (rootless maps the container UID to you, so the
  socket/secrets it writes are yours to open); the docker path works too.

Command construction is pure and unit-tested; the engine calls themselves are
exercised manually (they need a real Docker/Podman), like the systemd path.
"""

from __future__ import annotations

import getpass
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

from ..config.paths import ensure_home, moko_home
from ..theme import console
from .base import Runtime

# The monitor's in-container bind port (matches monitor.server.DEFAULT_PORT). We
# publish this to a host port (runtime.monitor_port), loopback-only.
INTERNAL_MONITOR_PORT = 4319
# The home of the unprivileged user inside the image (see Dockerfile). Moko's
# Claude Code creds mount here so `Path.home()/.claude` resolves to the volume.
CONTAINER_HOME = "/home/moko"
RESTART_POLICY = "unless-stopped"  # boot-persist: restart unless deliberately stopped

# Package managers we know how to install a container engine with. First match on PATH
# wins. brew (macOS) needs no sudo; the rest do. `{pkg}` is filled with the engine name.
_PKG_MANAGERS = (
    ("apt-get", ["sudo", "apt-get", "install", "-y", "{pkg}"]),
    ("dnf", ["sudo", "dnf", "install", "-y", "{pkg}"]),
    ("pacman", ["sudo", "pacman", "-S", "--noconfirm", "{pkg}"]),
    ("zypper", ["sudo", "zypper", "install", "-y", "{pkg}"]),
    ("brew", ["brew", "install", "{pkg}"]),
)


def _local_context() -> Path | None:
    """The build context (apps/agent, holding the Dockerfile + pyproject + uv.lock) when
    Moko runs from a source checkout — the editable/dev install. None when installed as a
    wheel, in which case we build straight from git instead."""
    root = Path(__file__).resolve().parents[3]  # runtime/ → moko/ → src/ → apps/agent/
    return root if (root / "Dockerfile").exists() else None


def _engine_install_cmd(engine: str) -> list[str] | None:
    """A copy-pasteable command to install `engine` via the first package manager on
    PATH, or None if we can't tell how. Engine install needs root (system package), so
    this is *offered with consent*, never run silently."""
    for tool, template in _PKG_MANAGERS:
        if shutil.which(tool):
            return [engine if part == "{pkg}" else part for part in template]
    return None


class ContainerRuntime(Runtime):
    def __init__(self, config):
        self.config = config
        rt = config.runtime
        self.bin = rt.mode if rt.mode in ("docker", "podman") else "podman"
        self.name = rt.container_name or "moko"
        self.image = rt.image or "moko:local"
        self.port = rt.monitor_port or INTERNAL_MONITOR_PORT
        self.mounts = list(rt.mounts or [])

    # ---- pure command construction (unit-tested) ----------------------------

    def _home(self) -> str:
        return str(moko_home())

    def run_argv(self) -> list[str]:
        """The `run -d` argv that brings the container up for the first time."""
        home = self._home()
        argv = [
            self.bin, "run", "-d",
            "--name", self.name,
            "--restart", RESTART_POLICY,
            "-e", f"MOKO_HOME={home}",
            "-e", "MOKO_MONITOR_HOST=0.0.0.0",
            # ~/.moko at the identical absolute path — socket + files cross the boundary.
            "-v", f"{home}:{home}",
            # Claude Code login persisted across rebuilds, mounted where Path.home() sees it.
            "-v", f"{home}/claude:{CONTAINER_HOME}/.claude",
            # Monitor: bound 0.0.0.0 inside, published to host loopback only.
            "-p", f"127.0.0.1:{self.port}:{INTERNAL_MONITOR_PORT}",
        ]
        for m in self.mounts:  # the companion's chosen M7 reach
            argv += ["-v", f"{m}:{m}"]
        argv.append(self.image)
        return argv

    def _argv(self, *args: str) -> list[str]:
        return [self.bin, *args]

    def exec_argv(self, *cmd: str, interactive: bool = True) -> list[str]:
        """argv to run a command *inside* the live container (e.g. the Claude login)."""
        flags = ["-it"] if interactive else []
        return [self.bin, "exec", *flags, self.name, *cmd]

    def build_argv(self) -> list[str]:
        """The `build` argv. From a local checkout we build the on-disk context; installed
        as a wheel we build straight from git (Dockerfile is auto-found in the subdir by
        both engines, so no -f is needed). $MOKO_REPO/$MOKO_REF override the git source,
        mirroring install.sh."""
        ctx = _local_context()
        if ctx is not None:
            return [self.bin, "build", "-t", self.image, str(ctx)]
        repo = os.environ.get("MOKO_REPO", "https://github.com/huesy/moko.git")
        ref = os.environ.get("MOKO_REF") or os.environ.get("MOKO_VERSION") or "main"
        return [self.bin, "build", "-t", self.image, f"{repo}#{ref}:apps/agent"]

    # ---- engine plumbing (patched in tests) ---------------------------------

    def _engine(self, *args: str, capture: bool = True) -> subprocess.CompletedProcess:
        return subprocess.run(list(args), text=True, capture_output=capture, check=False)

    def _inspect(self, fmt: str) -> str | None:
        """`<bin> inspect -f <fmt> <name>` → stripped stdout, or None if absent."""
        r = self._engine(self.bin, "inspect", "-f", fmt, self.name)
        if r.returncode != 0:
            return None
        return (r.stdout or "").strip()

    def _available(self) -> bool:
        return shutil.which(self.bin) is not None

    def _require_engine(self) -> bool:
        if self._available():
            return True
        console.print(
            f"[moko]{self.bin} isn't installed.[/moko] Install it, or switch tiers with "
            "[accent]moko config runtime.mode host[/accent]."
        )
        return False

    def _image_present(self) -> bool:
        return self._engine(self.bin, "image", "inspect", self.image).returncode == 0

    # ---- Runtime interface ---------------------------------------------------

    def _ensure_engine(self) -> bool:
        """Make sure the container engine is installed. If it's missing, *offer* to install
        it (a system package → needs root → consented, never a silent sudo): run it on a
        yes when we have a terminal, else print the exact command."""
        if self._available():
            return True
        cmd = _engine_install_cmd(self.bin)
        if cmd is None:
            console.print(
                f"[moko]{self.bin} isn't installed[/moko] and I couldn't find your package "
                f"manager. Install {self.bin}, then re-run [accent]moko service install[/accent]."
            )
            return False
        pretty = shlex.join(cmd)
        console.print(
            f"[moko]{self.bin} isn't installed.[/moko] It installs with:\n"
            f"  [accent]{pretty}[/accent]"
        )
        if not (sys.stdin.isatty() and sys.stdout.isatty()):
            console.print(
                "[muted]Run that, then re-run [accent]moko service install[/accent].[/muted]"
            )
            return False
        import questionary

        from ..theme import QUESTIONARY_STYLE

        if not questionary.confirm(
            f"Install {self.bin} now?", default=True, style=QUESTIONARY_STYLE
        ).ask():
            console.print("[muted]No problem — install it and re-run when you're ready.[/muted]")
            return False
        if subprocess.run(cmd, check=False).returncode != 0 or not self._available():
            console.print(
                f"[moko]That didn't complete.[/moko] Try it directly:\n  [accent]{pretty}[/accent]"
            )
            return False
        return True

    def _build_image(self) -> bool:
        """Build the image (streaming the build output). Automatic on first install so the
        companion never has to run `build` by hand."""
        argv = self.build_argv()
        where = "from source" if _local_context() is not None else "from git"
        console.print(
            f"[accent]Building [moko]{self.image}[/moko] {where} — first run, this can take a "
            "few minutes…[/accent] 🌸"
        )
        if self._engine(*argv, capture=False).returncode != 0:
            console.print(
                f"[moko]The image build failed.[/moko] You can run it directly:\n  "
                f"[accent]{shlex.join(argv)}[/accent]"
            )
            return False
        return True

    def _boot_persist(self) -> None:
        """Best-effort start-on-boot. Docker's --restart is handled by the docker daemon;
        rootless Podman needs the restart service + linger (both user-level, no root)."""
        if self.bin != "podman" or shutil.which("systemctl") is None:
            return
        subprocess.run(
            ["systemctl", "--user", "enable", "podman-restart.service"],
            capture_output=True, text=True, check=False,
        )
        if shutil.which("loginctl") is None:
            return
        r = subprocess.run(
            ["loginctl", "enable-linger", getpass.getuser()],
            capture_output=True, text=True, check=False,
        )
        if r.returncode == 0:
            console.print("[muted]Start-on-boot enabled (podman-restart + linger).[/muted]")
        else:
            console.print(
                "[muted]For start-on-boot, run: [accent]sudo loginctl enable-linger "
                f"{getpass.getuser()}[/accent].[/muted]"
            )

    def install(self) -> int:
        # Fully automatic: ensure the engine, build the image if needed, launch, boot-persist.
        if not self._ensure_engine():
            return 1
        ensure_home()
        (moko_home() / "claude").mkdir(mode=0o700, exist_ok=True)  # the mounted creds volume
        if not self._image_present() and not self._build_image():
            return 1
        self._engine(self.bin, "rm", "-f", self.name)  # replace any stale container
        r = self._engine(*self.run_argv())
        if r.returncode != 0:
            console.print(f"[moko]Couldn't start the container:[/moko] {(r.stderr or '').strip()}")
            return 1
        self._boot_persist()
        console.print(
            "[moko]Moko is running in a container.[/moko] 🌸  "
            "Check it with [accent]moko service status[/accent] or "
            "[accent]moko service logs[/accent]."
        )
        return 0

    def uninstall(self) -> int:
        if not self._require_engine():
            return 1
        self._engine(self.bin, "rm", "-f", self.name)
        console.print(
            f"Moko's container is removed. (The image [muted]{self.image}[/muted] and your "
            "mind under [muted]~/.moko[/muted] are left as-is.)"
        )
        return 0

    def _start_or_run(self) -> int:
        # Start the existing container; if it was never created, bring it up fresh.
        if self._inspect("{{.Id}}") is not None:
            r = self._engine(self.bin, "start", self.name)
        else:
            r = self._engine(*self.run_argv())
        if r.returncode != 0:
            console.print(f"[moko]Couldn't start Moko:[/moko] {(r.stderr or '').strip()}")
            return 1
        console.print("[moko]Moko is running.[/moko] 🌸")
        return 0

    def start(self) -> int:
        if not self._require_engine():
            return 1
        return self._start_or_run()

    def stop(self) -> int:
        if not self._require_engine():
            return 1
        r = self._engine(self.bin, "stop", self.name)
        if r.returncode != 0:
            console.print(f"[moko]Couldn't stop Moko:[/moko] {(r.stderr or '').strip()}")
            return r.returncode
        console.print("[moko]Moko has stopped.[/moko] 🌸")
        return 0

    def restart(self) -> int:
        if not self._require_engine():
            return 1
        r = self._engine(self.bin, "restart", self.name)
        if r.returncode != 0:
            console.print(f"[moko]Couldn't restart Moko:[/moko] {(r.stderr or '').strip()}")
            return r.returncode
        console.print("[moko]Moko is running.[/moko] 🌸")
        return 0

    def status(self) -> int:
        if not self._require_engine():
            return 1
        r = self._engine(
            self.bin, "ps", "-a", "--filter", f"name=^{self.name}$",
            "--format", "table {{.Names}}\t{{.Status}}\t{{.Ports}}", capture=True,
        )
        console.print((r.stdout or r.stderr).strip() or "No Moko container yet.")
        return 0

    def logs(self) -> int:
        if not self._require_engine():
            return 1
        console.print("[dim]Following Moko's logs — Ctrl+C to stop.[/dim]")
        import contextlib

        with contextlib.suppress(KeyboardInterrupt):
            self._engine(self.bin, "logs", "-f", "--tail", "100", self.name, capture=False)
        return 0

    def state(self) -> dict:
        if not self._available():
            return {"available": False, "installed": False, "active": False, "enabled": False}
        installed = self._inspect("{{.Id}}") is not None
        running = self._inspect("{{.State.Running}}") == "true"
        policy = self._inspect("{{.HostConfig.RestartPolicy.Name}}")
        return {
            "available": True,
            "installed": installed,
            "active": running,
            "enabled": bool(policy) and policy not in ("no", "<no value>"),
        }

    def status_line(self) -> str:
        s = self.state()
        if not s["available"]:
            return f"[muted]{self.bin} not installed — see [accent]moko doctor[/accent][/muted]"
        if not s["installed"]:
            return "[muted]not created — [accent]moko service install[/accent][/muted]"
        if s["active"]:
            return "[moko]running (container)[/moko]" + (
                " · starts on boot" if s["enabled"] else ""
            )
        return "[muted]created but stopped — [accent]moko service start[/accent][/muted]"

    def restart_quiet(self) -> bool:
        if not self._available():
            return False
        if self._engine(self.bin, "restart", self.name).returncode != 0:
            return False
        return self.state().get("active", False)
