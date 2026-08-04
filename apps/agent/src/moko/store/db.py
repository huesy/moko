"""Database connection + migration runner.

Migrations are numbered `.sql` files under ``moko/migrations`` applied in
order; the highest-applied version is recorded in ``schema_migrations``.
Migrations never change after they ship.
"""

from __future__ import annotations

import contextlib
import importlib.resources
import os

import aiosqlite

from ..clock import utc_isoformat
from ..log import get_logger
from . import backup

_log = get_logger("db")
_KEEP_SNAPSHOTS = 5  # how many pre-migration (auto) backups to retain


class SchemaTooNewError(RuntimeError):
    """The DB was migrated by a *newer* Moko than this code understands.

    The mind is one file, but two builds may open it — an editable host `moko`
    and a built container image (SANDBOX.md). If one migrates the DB forward, the
    older build must not operate a schema it doesn't know: it could read columns
    that moved or tables that changed shape. Fail closed, loudly, with the versions
    so the fix ("update this Moko") is obvious — never silently run a future schema.
    """


def _migration_files():
    root = importlib.resources.files("moko") / "migrations"
    files = [p for p in root.iterdir() if p.name.endswith(".sql")]
    return sorted(files, key=lambda p: int(p.name.split("_", 1)[0]))


def _known_max_version() -> int:
    """The highest migration this build ships (0 if none)."""
    return max((int(p.name.split("_", 1)[0]) for p in _migration_files()), default=0)


async def _snapshot(conn: aiosqlite.Connection, db_path: str, label: str) -> None:
    """Back up the whole DB to `<db_dir>/snapshots/<label>.db` before a migration.
    The graph is the irreplaceable moat; a bad migration must never be the thing
    that loses it. Best-effort — a failed snapshot must not block startup, but it's
    logged loudly. Prunes only *auto* (pre-v*) snapshots, never the operator's
    deliberate manual ones (see store.backup)."""
    try:
        target = await backup.create_snapshot(conn, db_path, label)
        backup.prune(db_path, backup.AUTO_PREFIX, _KEEP_SNAPSHOTS)
        _log.info("db: snapshotted to %s before migrating", target.name)
    except Exception:
        _log.warning("db: pre-migration snapshot failed (continuing)", exc_info=True)


async def _migrate(conn: aiosqlite.Connection, db_path: str = "") -> None:
    await conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations ("
        " version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
    )
    await conn.commit()
    cur = await conn.execute("SELECT version FROM schema_migrations")
    applied = {row[0] for row in await cur.fetchall()}

    # Version handshake: refuse a DB migrated beyond what this build knows, BEFORE
    # touching it. (A newer Moko ran here; this one would misread the schema.)
    applied_max = max(applied, default=0)
    known_max = _known_max_version()
    if applied_max > known_max:
        raise SchemaTooNewError(
            f"This mind was migrated to schema v{applied_max}, but this Moko only "
            f"understands up to v{known_max}. Update this Moko (or start the runtime "
            f"that migrated it) before opening the graph."
        )

    pending = [p for p in _migration_files() if int(p.name.split("_", 1)[0]) not in applied]
    # Snapshot before changing the schema of a DB that already holds data — never on
    # a fresh/empty init (nothing to lose), and never to ":memory:" (no parent dir).
    if pending and applied and db_path and db_path != ":memory:":
        top = max(int(p.name.split("_", 1)[0]) for p in pending)
        await _snapshot(conn, db_path, f"pre-v{top}")

    for path in pending:
        version = int(path.name.split("_", 1)[0])
        # All-or-nothing per migration. executescript() COMMITs any pending txn
        # then runs its script verbatim, so the transaction control must live *in*
        # the script — we wrap the DDL and its version row in one BEGIN/COMMIT.
        # A crash or error mid-file then leaves the DB untouched and re-runnable,
        # rather than half-applied (DDL done, version unrecorded) — which on the
        # next boot would hit "table already exists" and wedge startup for good.
        body = path.read_text(encoding="utf-8")
        script = (
            "BEGIN;\n"
            f"{body}\n"
            "INSERT INTO schema_migrations (version, applied_at) VALUES "
            f"({version}, '{utc_isoformat()}');\n"
            "COMMIT;"
        )
        try:
            await conn.executescript(script)
        except Exception:
            await conn.rollback()  # discard the half-applied transaction
            raise


def _pylower(s):
    """Deterministic Unicode-aware lower-casing, registered on the connection as the
    SQL function `pylower`. SQLite's built-in `lower()` is ASCII-only, so "Müller"
    and "MÜLLER" don't fold together in SQL — minting duplicate subjects. Using
    Python's `str.casefold()` on both the SQL side (via this function) and the
    argument keeps subject dedup Unicode-correct and consistent between the two."""
    return s.casefold() if s is not None else s


async def connect(path: str) -> aiosqlite.Connection:
    """Open (creating if needed) and migrate the database at ``path``."""
    conn = await aiosqlite.connect(path)
    # The graph holds the private mind (incl. private thoughts) — keep it owner-only
    # rather than the default 0644. Best-effort: ":memory:" and odd FSes just skip.
    with contextlib.suppress(OSError, TypeError):
        os.chmod(path, 0o600)
    conn.row_factory = aiosqlite.Row
    await conn.create_function("pylower", 1, _pylower, deterministic=True)
    await conn.execute("PRAGMA foreign_keys = ON")
    await conn.execute("PRAGMA journal_mode = WAL")
    # WAL lets readers and one writer coexist; busy_timeout makes a second *writer*
    # (a `moko ...` CLI command running while the daemon is mid-write) WAIT for the
    # lock instead of failing instantly with "database is locked". This is what
    # lets the CLI commands run directly against a live daemon — they queue behind
    # its writes rather than racing them. 5s comfortably covers a turn's writes.
    await conn.execute("PRAGMA busy_timeout = 5000")
    # A failed migration or a too-new-schema refusal must not leak the open
    # connection: its aiosqlite worker thread would outlive the caller and later
    # raise "Event loop is closed" at loop teardown. Close before propagating.
    try:
        await _migrate(conn, path)
    except BaseException:
        await conn.close()
        raise
    return conn
