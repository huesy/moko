"""The system tool: `execute_command` runs a shell command on the companion's
machine — the sharpest edge in the toolbox.

It is no longer a blanket "always ask": a deterministic classifier
(`shell.classify`) tiers each command, so everyday read/build/test/dev work runs
freely while installs, privilege, deletion, network egress, and the obvious
catastrophes are gated or refused. Commands run in Moko's work root
(`~/.moko/work`) by default, which bounds the blast radius of writes. Output is
captured and bounded.
"""

from __future__ import annotations

import asyncio
import os

from ..llm import Tool
from .base import SENSITIVE, AgentTool
from .process import SUPERVISOR
from .shell import classify, redact_secrets, referenced_secret_env, workspace

_TIMEOUT = 120.0
_MAX_OUTPUT = 12_000


async def _execute_command(args: dict) -> str:
    command = str(args.get("command") or "").strip()
    if not command:
        return "No command given."
    # Long-lived things (dev servers, tunnels) run in the background, tracked by
    # the supervisor; the classifier gated the command just the same.
    if args.get("background"):
        return await SUPERVISOR.start(str(args.get("name") or ""), command)
    cwd = workspace()
    # Inject only the secrets this command names ($NAME) — least privilege.
    env = {**os.environ, **referenced_secret_env(command)}
    try:
        proc = await asyncio.create_subprocess_shell(
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            cwd=str(cwd),
            env=env,
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=_TIMEOUT)
    except TimeoutError:
        return f"Command timed out after {_TIMEOUT:.0f}s."
    except Exception as exc:  # noqa: BLE001 — surface any spawn failure to the companion
        return f"Couldn't run it ({exc})."
    text = (out or b"").decode("utf-8", "replace")
    text = text[:_MAX_OUTPUT] + ("\n…(truncated)" if len(text) > _MAX_OUTPUT else "")
    # Redact any secret value that leaked into output before Moko sees it.
    return redact_secrets(f"(exit {proc.returncode})\n{text}".strip())


EXECUTE_COMMAND = AgentTool(
    spec=Tool(
        name="execute_command",
        description=(
            "Run a shell command on the companion's machine. Runs in Moko's work "
            "folder (~/.moko/work) by default. Everyday read/build/test/dev commands "
            "run immediately; installing software, system changes, deletion, and "
            "sending data off the machine need the companion's okay first. For "
            "long-lived things (a dev server, a tunnel), set background=true and give "
            "a short name, then manage it with the `processes` tool."
        ),
        parameters={
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "the shell command"},
                "background": {
                    "type": "boolean",
                    "description": "keep it running in the background (dev server, tunnel)",
                },
                "name": {"type": "string", "description": "short name for a background process"},
            },
            "required": ["command"],
        },
    ),
    category="system",
    risk=SENSITIVE,
    run=_execute_command,
    classify=classify,
)

SYSTEM_TOOLS = [EXECUTE_COMMAND]
