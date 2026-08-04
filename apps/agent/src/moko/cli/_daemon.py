"""Talk to the running Moko daemon over its local control socket.

So a `moko outreach/reflect/research/dream` command is executed *by the live
daemon* — one writer, and it shows in the monitor feed like any beat — instead of
spinning up a second Moko in the CLI process.

The load-bearing distinction (see SANDBOX.md): we separate "no daemon is
reachable" from "a daemon is here but slow". If we *connected*, a daemon is
alive and owns the DB — a slow reply is `BUSY`, never a reason to fall back and
run the action a second time in-process. Only a genuine failure to connect is
`UNREACHABLE`. `try_command` returns None solely for the UNREACHABLE case, so the
caller can decide whether an in-process fallback is safe (it is on `host`; it is
NOT in a container, where a second mind would race the shared, bind-mounted DB).
"""

from __future__ import annotations

import asyncio
import contextlib

from rich.console import Console

from ..config.paths import COMMAND_READ_TIMEOUT_S, control_socket_path
from ..theme import RICH_THEME, console

# A touch over the daemon's own command timeout (LLM-heavy actions), so the socket
# read never gives up before the daemon would. Paired in config.paths so it and
# the daemon's run budget can't drift.
_READ_TIMEOUT = COMMAND_READ_TIMEOUT_S

# A second console on stderr, so a failed command's message doesn't pollute the
# stdout a caller might be capturing (and so `moko dream 2>err` behaves).
_err_console = Console(theme=RICH_THEME, stderr=True)

# Dispatch outcomes.
_OK = "ok"  # connected, the daemon ran it and reported success ('+' prefix)
_ERR = "err"  # connected, the daemon ran it but it failed ('-' prefix) → exit 1
_BUSY = "busy"  # connected, but the daemon is still working (slow / dropped mid-reply)
_UNREACHABLE = "unreachable"  # nothing to connect to (no socket, or a stale/unopenable one)


async def _dispatch(action: str) -> tuple[str, str | None]:
    """Hand `action` to the daemon. Returns (outcome, payload):

    - (_OK, result_line)   — a daemon ran it and reported success.
    - (_ERR, result_line)  — a daemon ran it but the action failed; the caller exits 1.
    - (_BUSY, None)        — a daemon is listening but hasn't replied in time, or the
                             connection dropped mid-exchange. It's alive; do NOT re-run.
    - (_UNREACHABLE, None) — nothing is listening (no socket, or a socket we can't open,
                             e.g. a crashed daemon's leftover, or a UID-remapped one).

    The reply carries a 1-byte status prefix ('+' ok / '-' error) before the line;
    we split it into the _OK/_ERR outcome so a scripted `moko dream && …` sees the
    real result instead of always succeeding.
    """
    path = control_socket_path()
    if not path.exists():
        return (_UNREACHABLE, None)
    try:
        reader, writer = await asyncio.open_unix_connection(str(path))
    except (OSError, NotImplementedError):
        # The socket file is there but won't connect: a stale socket from a crashed
        # daemon, or (Docker rootless) one owned by a subordinate UID we can't open.
        # Either way it is NOT "no daemon" — the caller must not assume it's safe to run.
        return (_UNREACHABLE, None)
    # Connected → a daemon is alive and holds the DB. From here we never report
    # UNREACHABLE: a timeout or a dropped connection is BUSY (it's working), so the
    # caller won't spawn a second in-process mind against the same store.
    try:
        writer.write((action + "\n").encode("utf-8"))
        await writer.drain()
        data = await asyncio.wait_for(reader.read(), timeout=_READ_TIMEOUT)
        line = data.decode("utf-8", "replace").strip()
        if line[:1] == "-":
            return (_ERR, line[1:])
        # '+' prefix (or, defensively, an unprefixed line from an older daemon) → ok.
        return (_OK, line[1:] if line[:1] == "+" else line)
    except (TimeoutError, OSError):
        return (_BUSY, None)
    finally:
        with contextlib.suppress(Exception):
            writer.close()
            await writer.wait_closed()


def try_command(action: str) -> int | None:
    """If a daemon is listening, have it run `action`, report, and return an exit code.
    Return None *only* when no daemon is reachable, so the caller decides whether an
    in-process fallback is safe. A slow-but-alive daemon returns 0 (it owns the action),
    never None — so we never double-run it. A daemon-run action that *failed* returns 1
    (printed to stderr), so a scripted `moko dream && …` stops on the failure."""
    outcome, payload = asyncio.run(_dispatch(action))
    if outcome == _OK:
        console.print(f"[moko]{payload}[/moko]")
        return 0
    if outcome == _ERR:
        _err_console.print(f"[moko]{payload}[/moko]")
        return 1
    if outcome == _BUSY:
        console.print(
            "[moko]Moko is still working on that in the background.[/moko] "
            "[muted]Watch the monitor or `moko service logs` for the result.[/muted]"
        )
        return 0
    return None  # UNREACHABLE → caller falls back (host) or refuses (container)


async def _ping() -> bool:
    """True iff a daemon is actually listening on the control socket (not just a
    stale socket file)."""
    path = control_socket_path()
    if not path.exists():
        return False
    try:
        _reader, writer = await asyncio.open_unix_connection(str(path))
    except (OSError, NotImplementedError):
        return False
    with contextlib.suppress(Exception):
        writer.close()
        await writer.wait_closed()
    return True


def is_daemon_live() -> bool:
    """Whether the background daemon is running and holding the DB — used to refuse
    operations (like a restore) that can't be done safely while it's open."""
    return asyncio.run(_ping())
