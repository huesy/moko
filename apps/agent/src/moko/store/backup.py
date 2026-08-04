"""Backup + restore of the whole mind (the SQLite graph).

The graph is Moko's irreplaceable moat — losing it loses *everything* it has
learned, with no way to reconstruct it. Two layers protect it:

  • **Automatic** — `db._snapshot` takes a clean copy before any migration
    changes the schema (a bad migration must never be the thing that loses it).
  • **On demand** — the operator can snapshot before a risky change, list what
    exists, and restore one if something goes wrong.

This module is the shared machinery for both. A snapshot is a standalone,
consistent SQLite file produced by ``VACUUM INTO`` (no ``-wal``/``-shm``
sidecars to carry around), stored under ``<db_dir>/snapshots/`` owner-only.

Restore is deliberately conservative: it refuses a corrupt snapshot, backs up
the *current* mind first (so the restore is itself reversible), and re-opens
through the normal migration path so an older snapshot is brought up to the
current schema. The caller (the `moko restore` CLI) is responsible for ensuring
the daemon is stopped first — you cannot safely swap the file out from under a
live writer.
"""

from __future__ import annotations

import contextlib
import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path

import aiosqlite

from ..clock import now_utc
from ..log import get_logger

_log = get_logger("backup")

# Filename prefixes that tell snapshots apart by *why* they exist. Auto-snapshots
# are transient safety nets pruned to the last N; manual ones are deliberate and
# kept until the operator removes them; pre-restore ones are the undo for a restore.
AUTO_PREFIX = "pre-v"  # pre-v<N>.db — taken before applying migration N
MANUAL_PREFIX = "manual-"  # manual-<stamp>.db — `moko snapshot` with no label
RESTORE_BACKUP_PREFIX = "pre-restore-"  # pre-restore-<stamp>.db — the undo for a restore

_LABEL_OK = re.compile(r"[^A-Za-z0-9._-]+")


def snapshots_dir(db_path: str | Path) -> Path:
    """Where snapshots live for a given DB: a `snapshots/` sibling of the file."""
    return Path(db_path).parent / "snapshots"


def _exists(path: str | Path) -> bool:
    """A path existence check that doesn't live in an async frame — the only fs
    metadata ops here are trivial stats on a local file, but keeping them out of the
    coroutine keeps the loop-blocking linter honest about what it can see."""
    return Path(path).exists()


def fs_stamp() -> str:
    """A filesystem-safe UTC timestamp (`20260626T143000Z`) for snapshot names —
    sortable and collision-free within a second."""
    return now_utc().strftime("%Y%m%dT%H%M%SZ")


def sanitize_label(label: str) -> str:
    """Make an operator-supplied label safe as a filename (no path traversal, no
    spaces). Empty/odd input falls back to a timestamped manual label."""
    cleaned = _LABEL_OK.sub("-", label.strip()).strip("-.")
    return cleaned or f"{MANUAL_PREFIX}{fs_stamp()}"


def kind_of(name: str) -> str:
    """Classify a snapshot file by its prefix, for display + targeted pruning."""
    if name.startswith(AUTO_PREFIX):
        return "auto"
    if name.startswith(RESTORE_BACKUP_PREFIX):
        return "pre-restore"
    return "manual"


@dataclass(frozen=True)
class SnapshotInfo:
    path: Path
    name: str  # filename without the .db suffix — what `moko restore` takes
    size: int  # bytes
    mtime: float  # epoch seconds
    kind: str  # "auto" | "manual" | "pre-restore"


def list_snapshots(db_path: str | Path) -> list[SnapshotInfo]:
    """Every snapshot on disk, newest first."""
    d = snapshots_dir(db_path)
    if not d.exists():
        return []
    out: list[SnapshotInfo] = []
    for p in d.glob("*.db"):
        st = p.stat()
        out.append(SnapshotInfo(p, p.stem, st.st_size, st.st_mtime, kind_of(p.name)))
    out.sort(key=lambda s: s.mtime, reverse=True)
    return out


def prune(db_path: str | Path, prefix: str, keep: int) -> None:
    """Keep only the most recent `keep` snapshots whose name starts with `prefix`,
    deleting older ones. Scoped by prefix so auto-pruning never touches the
    operator's deliberate (manual) snapshots."""
    d = snapshots_dir(db_path)
    if not d.exists():
        return
    snaps = sorted(d.glob(f"{prefix}*.db"), key=lambda p: p.stat().st_mtime)
    for old in snaps[: max(0, len(snaps) - keep)]:
        old.unlink(missing_ok=True)


async def create_snapshot(conn: aiosqlite.Connection, db_path: str | Path, label: str) -> Path:
    """Write a consistent copy of the DB behind `conn` to `<snapshots>/<label>.db`
    via ``VACUUM INTO``. Used by the daemon (which passes its live store connection
    so the copy is taken cleanly between writes on the serial consumer)."""
    d = snapshots_dir(db_path)
    d.mkdir(parents=True, exist_ok=True)
    target = d / f"{label}.db"
    if target.exists():
        target.unlink()
    # Commit any pending work first — VACUUM INTO requires no open transaction.
    with contextlib.suppress(Exception):
        await conn.commit()
    await conn.execute("VACUUM INTO ?", (str(target),))
    with contextlib.suppress(OSError):
        os.chmod(target, 0o600)  # a backup of the private mind is just as private
    return target


async def snapshot_now(db_path: str | Path, label: str) -> Path:
    """Snapshot the DB at `db_path` from a fresh read connection — the in-process
    path for `moko snapshot` when no daemon is up to do it. Raises FileNotFoundError
    if there's no mind yet."""
    if not _exists(db_path):
        raise FileNotFoundError("there's no mind to snapshot yet")
    conn = await aiosqlite.connect(str(db_path))
    try:
        return await create_snapshot(conn, db_path, label)
    finally:
        await conn.close()


async def integrity_ok(path: str | Path) -> bool:
    """True iff the SQLite file passes ``PRAGMA integrity_check`` — a corrupt
    snapshot must never be restored over a working mind."""
    try:
        conn = await aiosqlite.connect(str(path))
    except Exception:
        return False
    try:
        cur = await conn.execute("PRAGMA integrity_check")
        row = await cur.fetchone()
        return bool(row) and row[0] == "ok"
    except Exception:
        return False
    finally:
        with contextlib.suppress(Exception):
            await conn.close()


async def _looks_like_a_mind(path: str | Path) -> bool:
    """A light guard that the file is actually a Moko graph (has its tables), so a
    restore from the wrong file fails clearly here rather than mid-migration."""
    try:
        conn = await aiosqlite.connect(str(path))
    except Exception:
        return False
    try:
        cur = await conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name IN ('schema_migrations', 'subjects')"
        )
        return len(await cur.fetchall()) > 0
    except Exception:
        return False
    finally:
        with contextlib.suppress(Exception):
            await conn.close()


def _sidecars(db_path: str | Path) -> list[Path]:
    """The WAL/SHM files that ride alongside a live DB. They belong to the *old*
    contents and must be cleared when the underlying file is swapped."""
    base = str(db_path)
    return [Path(base + "-wal"), Path(base + "-shm")]


async def resolve(db_path: str | Path, name: str) -> Path | None:
    """Find a snapshot by name (with or without the .db suffix). None if missing."""
    fname = name if name.endswith(".db") else f"{name}.db"
    cand = snapshots_dir(db_path) / fname
    return cand if cand.exists() else None


async def restore_snapshot(db_path: str | Path, name: str) -> str:
    """Replace the live mind with snapshot `name`, safely. The current mind is
    backed up first (so this is reversible), the snapshot is integrity-checked
    before anything is overwritten, and the result is re-opened through the
    migration path so an older snapshot is brought up to the current schema.

    The CALLER must ensure no daemon is holding the DB — restore swaps the file.
    Returns a human-readable summary. Raises on a missing/corrupt snapshot.
    """
    db = Path(db_path)
    src = await resolve(db_path, name)
    if src is None:
        raise FileNotFoundError(f"no snapshot named {name!r}")
    if not await integrity_ok(src):
        raise ValueError(f"snapshot {name!r} failed its integrity check — refusing to restore")
    if not await _looks_like_a_mind(src):
        raise ValueError(f"{name!r} doesn't look like a Moko mind — refusing to restore")

    # 1) Back up the CURRENT mind first, so a restore is itself undoable. VACUUM INTO
    #    captures it consistently (folding in any pending WAL).
    backup_note = "no existing mind to back up"
    if _exists(db):
        backup = snapshots_dir(db_path) / f"{RESTORE_BACKUP_PREFIX}{fs_stamp()}.db"
        conn = await aiosqlite.connect(str(db))
        try:
            await conn.execute("VACUUM INTO ?", (str(backup),))
        finally:
            await conn.close()
        with contextlib.suppress(OSError):
            os.chmod(backup, 0o600)
        backup_note = f"previous mind saved to {backup.name}"

    # 2) Swap the file in atomically: copy to a temp sibling, then os.replace() into
    #    place (atomic on the same filesystem). A crash mid-copy then leaves the
    #    original mind untouched rather than half-overwritten. Discard the old
    #    WAL/SHM — they belong to the prior DB and would corrupt the new file.
    db.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(str(db) + ".restoring")
    shutil.copyfile(src, staging)
    with contextlib.suppress(OSError):
        os.chmod(staging, 0o600)
    os.replace(staging, db)
    for side in _sidecars(db):
        side.unlink(missing_ok=True)

    # 3) Re-open through the normal path: this migrates an older snapshot forward
    #    to the current schema and leaves a clean, checkpointed DB. Import lazily to
    #    avoid a module cycle (db imports this module for its auto-snapshot).
    from .db import connect

    conn = await connect(str(db))
    try:
        cur = await conn.execute("PRAGMA integrity_check")
        row = await cur.fetchone()
        if not (row and row[0] == "ok"):
            raise RuntimeError("restored DB failed its integrity check after re-open")
    finally:
        await conn.close()

    _log.info("restored mind from snapshot %s (%s)", name, backup_note)
    return f"Restored the mind from {name!r}; {backup_note}."
