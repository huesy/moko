"""Filesystem tools — deliberately just two: `read_file` and `write_file`.

These earn their place over the shell on *correctness*, which is the only reason
to keep a dedicated tool alongside a general shell (the lesson from Claude Code /
Hermes / OpenClaw): `read_file` bounds and structures output, and `write_file`
writes multi-line content reliably without shell heredoc/quoting fragility.
Listing, stat, and delete are *not* here — `ls` / `stat` / `rm` through
`execute_command` do them, gated by the same workspace boundary, so there's one
resource policy and nothing to route around.

Consent gates on the **resource** (the path), shared with the shell classifier:
free inside Moko's work folder, asked outside it, refused on credential paths."""

from __future__ import annotations

from pathlib import Path

from ..llm import Tool
from .base import SAFE, SENSITIVE, AgentTool, GateDecision
from .consent import ALLOW, ASK, DENY
from .shell import BLOCKED, FREE, GATE, _expand, in_workspace, is_secret_path, workspace

_MAX_READ = 12_000  # chars of a file returned to the model


def _resolve(path: str) -> Path | None:
    """Resolve a path, or None if it's malformed or a credential/key store.

    A *relative* path anchors to Moko's workspace — not the daemon's CWD — so it
    matches the boundary `_classify_write` cleared: `write_file('notes.txt')`
    lands in ~/.moko/work, never in whatever directory systemd happened to launch
    the service from (which it would silently overwrite under an 'in your
    workspace' verdict)."""
    try:
        # Expand $HOME/${HOME} then ~ exactly as the shell classifier's
        # `shell._resolved` does (via `shell._expand`), so the path the writer
        # lands on matches the one `_classify_write` gated. Without the $HOME
        # step, `write_file('$HOME/x')` gates as outside-workspace (ASK) yet, on
        # approval, wrote a *literal* `$HOME/` dir inside the workspace.
        p = Path(_expand(path)).expanduser()
        if not p.is_absolute():
            p = workspace() / p
        resolved = p.resolve()
    except (ValueError, OSError, RuntimeError):
        return None
    return None if is_secret_path(str(resolved)) else resolved


async def _read_file(args: dict) -> str:
    target = _resolve(str(args.get("path") or ""))
    if target is None:
        return "I'm not allowed to read that (it looks sensitive or is invalid)."
    if not target.is_file():
        return f"{target} isn't a readable file."
    try:
        text = target.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return f"Couldn't read it ({exc})."
    return text[:_MAX_READ] + ("\n…(truncated)" if len(text) > _MAX_READ else "")


async def _write_file(args: dict) -> str:
    target = _resolve(str(args.get("path") or ""))
    if target is None:
        return "I can't write there (blocked or invalid path)."
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(str(args.get("content") or ""), encoding="utf-8")
    except OSError as exc:
        return f"Couldn't write it ({exc})."
    return f"Wrote {target}."


def _classify_write(args: dict, policy: dict[str, str] | None = None) -> GateDecision:
    """Workspace-aware consent for a write — the same boundary (and the same trust
    key) the shell uses, so `write_file` and a shell redirect to the same path get
    the same answer, and one "always" on `outside_write` frees both surfaces."""
    pol = policy or {}
    path = str(args.get("path") or "")
    if not path:
        return GateDecision(ASK, "no path given", GATE)
    if is_secret_path(path):
        return GateDecision(DENY, "that path is off-limits (credential/key store)", BLOCKED)
    if in_workspace(path):
        return GateDecision(ALLOW, "in your workspace", FREE)
    if pol.get("shell") == "deny":
        return GateDecision(DENY, "writing is switched off", GATE, "outside_write")
    verdict = ALLOW if pol.get("outside_write") == "allow" else ASK
    return GateDecision(verdict, "outside your workspace", GATE, "outside_write")


_PATH = {"path": {"type": "string", "description": "an absolute or ~ path"}}

READ_FILE = AgentTool(
    spec=Tool(
        name="read_file",
        description="Read a text file's contents (bounded). For listing/stat, use execute_command.",
        parameters={"type": "object", "properties": _PATH, "required": ["path"]},
    ),
    category="file", risk=SAFE, run=_read_file,
)
WRITE_FILE = AgentTool(
    spec=Tool(
        name="write_file",
        description=(
            "Write (overwrite) a text file — the reliable way to write multi-line "
            "content. Runs freely in your work folder (~/.moko/work); writing "
            "elsewhere needs the companion's approval."
        ),
        parameters={
            "type": "object",
            "properties": {**_PATH, "content": {"type": "string"}},
            "required": ["path", "content"],
        },
    ),
    category="file", risk=SENSITIVE, run=_write_file, classify=_classify_write,
)

FILE_TOOLS = [READ_FILE, WRITE_FILE]
