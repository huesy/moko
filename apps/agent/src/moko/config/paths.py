"""Where Moko keeps its things on disk.

Everything Moko owns lives under ~/.moko/ (overridable with $MOKO_HOME,
which is handy for tests). The repo never holds runtime state.
"""

from __future__ import annotations

import contextlib
import os
from pathlib import Path

# A commanded action (`moko dream/outreach/…`, handed to the daemon over the
# control socket) may make LLM calls, so it gets a generous budget to finish.
# The daemon caps the *run* at this; the CLI waits a touch longer for the reply
# (COMMAND_READ_TIMEOUT_S) so the socket read never gives up before the daemon
# itself would. Paired here, in the one place both the gateway and the CLI
# already import, so the two can't drift.
COMMAND_TIMEOUT_S = 150
COMMAND_READ_TIMEOUT_S = COMMAND_TIMEOUT_S + 10


def moko_home() -> Path:
    """The root of Moko's on-disk home."""
    env = os.environ.get("MOKO_HOME")
    return Path(env).expanduser() if env else Path.home() / ".moko"


def db_path() -> Path:
    """The graph database — Moko's mind on disk."""
    return moko_home() / "moko.db"


def config_path() -> Path:
    """The config file Moko reads at startup."""
    return moko_home() / "config.yaml"


def logs_dir() -> Path:
    """Where Moko writes its logs."""
    return moko_home() / "logs"


def skills_dir() -> Path:
    """Where the companion's own skills live (alongside Moko's built-in ones)."""
    return moko_home() / "skills"


def media_dir() -> Path:
    """Where Moko keeps binary artifacts (images, voice notes, documents) it's been
    sent or has generated. The bytes live here, content-addressed; the graph holds
    the sourced record. Kept out of SQLite so the DB stays small and the blobs stay
    on the companion's machine."""
    return moko_home() / "media"


def control_socket_path() -> Path:
    """The daemon's local control socket — how a `moko …` CLI command hands its
    action to the running daemon (so the daemon executes it, not a second process)."""
    return moko_home() / "control.sock"


def ensure_home() -> Path:
    """Create the home (and logs + skills + media) directories if needed; return the home."""
    home = moko_home()
    home.mkdir(parents=True, exist_ok=True)
    logs_dir().mkdir(parents=True, exist_ok=True)
    skills_dir().mkdir(parents=True, exist_ok=True)
    media_dir().mkdir(parents=True, exist_ok=True)
    # The mind is private (the whole premise: "the companion's data stays on their
    # machine"). Keep the home and its subdirs owner-only, so the graph DB, config,
    # logs, and media aren't world-readable on a shared host — mkdir respects the
    # umask, so set it explicitly rather than trusting it.
    for d in (home, logs_dir(), skills_dir(), media_dir()):
        with contextlib.suppress(OSError):
            os.chmod(d, 0o700)
    return home
