"""Long-lived background processes — Moko's process supervisor.

`execute_command` runs a command to completion. Some things need to keep running:
a dev server, an ngrok tunnel, a TTS daemon. Those start in the *background* here
— tracked, their output captured to a log, stoppable by name. One supervisor
(module singleton) holds them for the life of the agent; a small on-disk registry
lets `list`/`logs`/`stop` keep working and shows what died if Moko restarts.

Starting a background process still runs a shell command, so it goes through the
same classifier gate as `execute_command` (binding a port, running a tunnel →
gated once). Managing them — list/logs/stop of Moko's *own* tracked processes —
is read/control only, so the `processes` tool itself is safe.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import signal
from dataclasses import asdict, dataclass
from pathlib import Path

from ..clock import now_utc, utc_isoformat
from ..config.paths import moko_home
from ..events import publish as _publish_event
from ..llm import Tool
from .base import SAFE, AgentTool
from .shell import redact_secrets, referenced_secret_env, workspace

_NAME_MAX = 48
_STOP_GRACE_S = 2.0  # SIGTERM, wait this long, then SIGKILL


def _proc_dir() -> Path:
    d = moko_home() / "proc"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, just not ours to signal
    return True


@dataclass
class Proc:
    name: str
    command: str
    pid: int
    cwd: str
    started_at: str  # ISO UTC
    log: str
    status: str = "running"  # running | exited | stopped
    exit_code: int | None = None


class ProcessSupervisor:
    """Tracks background processes Moko has started. One instance per agent."""

    def __init__(self) -> None:
        self._procs: dict[str, Proc] = {}
        self._handles: dict[str, asyncio.subprocess.Process] = {}
        self._loaded = False

    # ── persistence ──────────────────────────────────────────────────────────
    def _registry(self) -> Path:
        return _proc_dir() / "registry.json"

    def _load(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        path = self._registry()
        if not path.exists():
            return
        try:
            rows = json.loads(path.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            return
        for row in rows if isinstance(rows, list) else []:
            try:
                p = Proc(**row)
            except TypeError:
                continue
            # We restarted and no longer hold the handle: if its PID is gone it
            # died with us; surface that rather than claiming it's still running.
            if p.status == "running" and not _alive(p.pid):
                p.status = "stopped"
            self._procs[p.name] = p

    def _save(self) -> None:
        with contextlib.suppress(OSError):
            self._registry().write_text(
                json.dumps([asdict(p) for p in self._procs.values()], indent=2),
                encoding="utf-8",
            )

    # ── lifecycle ────────────────────────────────────────────────────────────
    async def start(self, name: str, command: str, cwd: str | None = None) -> str:
        self._load()
        name = (name or "").strip()[:_NAME_MAX] or f"proc-{now_utc():%H%M%S}"
        existing = self._procs.get(name)
        if existing and existing.status == "running" and _alive(existing.pid):
            return f"A process named '{name}' is already running (pid {existing.pid})."
        work = str(cwd or workspace())
        log_path = _proc_dir() / f"{name}.log"
        logf = log_path.open("wb")  # held open for the process's lifetime; closed in _reap
        env = {**os.environ, **referenced_secret_env(command)}  # least-privilege secret access
        try:
            proc = await asyncio.create_subprocess_shell(
                command,
                stdout=logf,
                stderr=asyncio.subprocess.STDOUT,
                cwd=work,
                env=env,
                start_new_session=True,  # own process group → clean group-signal on stop
            )
        except Exception as exc:  # noqa: BLE001 — surface a spawn failure to the companion
            logf.close()
            return f"Couldn't start '{name}' ({exc})."
        p = Proc(name, command, proc.pid, work, utc_isoformat(), str(log_path))
        self._procs[name] = p
        self._handles[name] = proc
        self._save()
        _publish_event({"type": "process.start", "name": name, "pid": proc.pid, "command": command})
        asyncio.create_task(self._reap(name, proc, logf))
        await asyncio.sleep(0.4)  # a beat, so a fast crash / startup banner shows
        tail = self._tail(p, lines=10)
        verb = "Started" if self._procs[name].status == "running" else "Ran (already exited):"
        return f"{verb} '{name}' (pid {proc.pid}).\n{tail}".strip()

    async def _reap(self, name: str, proc: asyncio.subprocess.Process, logf) -> None:
        code = await proc.wait()
        with contextlib.suppress(Exception):
            logf.close()
        p = self._procs.get(name)
        if p is not None and p.status == "running":
            p.status = "exited"
            p.exit_code = code
            self._save()
        self._handles.pop(name, None)
        _publish_event({"type": "process.exit", "name": name, "code": code})

    async def stop(self, name: str) -> str:
        self._load()
        p = self._procs.get(name)
        if p is None:
            return f"No process named '{name}'."
        if p.status != "running" or not _alive(p.pid):
            p.status = "stopped"
            self._save()
            return f"'{name}' wasn't running."
        self._signal_group(p.pid, signal.SIGTERM)
        for _ in range(int(_STOP_GRACE_S / 0.1)):
            await asyncio.sleep(0.1)
            if not _alive(p.pid):
                break
        else:
            self._signal_group(p.pid, signal.SIGKILL)
        p.status = "stopped"
        self._save()
        _publish_event({"type": "process.stop", "name": name})
        return f"Stopped '{name}'."

    @staticmethod
    def _signal_group(pid: int, sig: int) -> None:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(os.getpgid(pid), sig)

    # ── reads ────────────────────────────────────────────────────────────────
    def list(self) -> list[dict]:
        self._load()
        out: list[dict] = []
        for p in self._procs.values():
            if p.status == "running" and not _alive(p.pid):
                p.status = "exited"
            out.append(
                {
                    "name": p.name,
                    "command": p.command,
                    "pid": p.pid,
                    "status": p.status,
                    "started_at": p.started_at,
                    "exit_code": p.exit_code,
                }
            )
        return out

    def _tail(self, p: Proc, lines: int) -> str:
        try:
            text = Path(p.log).read_text(encoding="utf-8", errors="replace")
        except OSError:
            return "(no output yet)"
        tail = "\n".join(text.splitlines()[-lines:]) or "(no output yet)"
        return redact_secrets(tail)  # never surface a secret a process printed

    def logs(self, name: str, lines: int = 40) -> str:
        self._load()
        p = self._procs.get(name)
        if p is None:
            return f"No process named '{name}'."
        return self._tail(p, lines)


# Module singleton — shared across tool calls for the life of the agent.
SUPERVISOR = ProcessSupervisor()


# ── the `processes` management tool (safe: list/logs/stop only) ───────────────
async def _manage_processes(args: dict) -> str:
    action = str(args.get("action") or "list").strip().lower()
    name = str(args.get("name") or "").strip()
    if action == "list":
        procs = SUPERVISOR.list()
        if not procs:
            return "No background processes."
        lines = [
            f"• {p['name']} [{p['status']}] pid {p['pid']} — {p['command']}"
            + (f" (exit {p['exit_code']})" if p["exit_code"] is not None else "")
            for p in procs
        ]
        return "\n".join(lines)
    if not name:
        return f"Which process? Give a name for '{action}'."
    if action == "logs":
        lines = args.get("lines")
        n = int(lines) if isinstance(lines, int) or str(lines).isdigit() else 40
        return SUPERVISOR.logs(name, lines=max(1, min(n, 500)))
    if action == "stop":
        return await SUPERVISOR.stop(name)
    return f"Unknown action '{action}'. Use list, logs, or stop."


PROCESSES = AgentTool(
    spec=Tool(
        name="processes",
        description=(
            "Manage the background processes you've started (dev servers, tunnels, "
            "daemons). action='list' shows them; 'logs' tails one's output (give "
            "name, optional lines); 'stop' ends one (give name). To START one, use "
            "execute_command with background=true."
        ),
        parameters={
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["list", "logs", "stop"]},
                "name": {"type": "string", "description": "process name (for logs/stop)"},
                "lines": {"type": "integer", "description": "log lines to show (logs)"},
            },
            "required": ["action"],
        },
    ),
    category="system",
    risk=SAFE,
    run=_manage_processes,
)

PROCESS_TOOLS = [PROCESSES]
