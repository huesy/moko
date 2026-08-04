"""The schema-version handshake (SANDBOX.md): one mind file, possibly two builds.
A Moko that only knows up to schema vN must refuse a DB some newer Moko already
migrated to v>N, rather than silently misreading a future schema.
"""

from __future__ import annotations

import aiosqlite
import pytest

from moko.clock import utc_isoformat
from moko.store import SchemaTooNewError, Store
from moko.store.db import _known_max_version


async def test_opens_normally_at_current_version(tmp_path):
    store = await Store.open(tmp_path / "ok.db")  # migrates to the known max; no raise
    await store.close()


async def test_refuses_a_future_migrated_db(tmp_path):
    db = tmp_path / "future.db"
    store = await Store.open(db)
    await store.close()

    # Simulate a newer Moko having recorded a migration this build doesn't ship.
    future = _known_max_version() + 5
    conn = await aiosqlite.connect(str(db))
    await conn.execute(
        "INSERT INTO schema_migrations (version, applied_at) VALUES (?, ?)",
        (future, utc_isoformat()),
    )
    await conn.commit()
    await conn.close()

    with pytest.raises(SchemaTooNewError) as excinfo:
        await Store.open(db)
    assert str(future) in str(excinfo.value)  # the message names the offending version
