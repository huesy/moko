"""M10 — the media layer (store.add_media + blob persistence).

A media artifact is a fact in the provenance sense: it must be sourced, time-
stamped, and recoverable. These prove the bytes land on disk content-addressed,
the row points at a real source, and identical files share one blob.
"""

import pytest

from moko.config.paths import media_dir
from moko.store import Store
from moko.store import blobs as media_blobs


@pytest.fixture
async def store(tmp_path):
    s = await Store.open(tmp_path / "moko.db")
    try:
        yield s
    finally:
        await s.close()


async def test_add_media_persists_blob_and_sources_it(store):
    msg = await store.add_message("telegram", "in", "look")
    art = await store.add_media(
        b"\xff\xd8\xffphoto-bytes", kind="image", mime="image/jpeg", filename="cat.jpg",
        caption="my cat", message_id=msg.id, channel="telegram", ref="file123",
    )
    # The bytes are on disk, content-addressed, and match.
    blob = media_dir() / art.path
    assert blob.exists()
    assert art.path.startswith(art.sha256)
    assert blob.read_bytes() == b"\xff\xd8\xffphoto-bytes"
    assert art.bytes == len(b"\xff\xd8\xffphoto-bytes")
    # And it's sourced — provenance is mandatory, even for what Moko is shown.
    cur = await store.conn.execute(
        "SELECT kind, channel FROM sources WHERE id = ?", (art.source_id,)
    )
    row = await cur.fetchone()
    assert row is not None and row["channel"] == "telegram"


async def test_identical_files_share_one_blob(store):
    a = await store.add_media(b"same-bytes", kind="image", mime="image/png")
    b = await store.add_media(b"same-bytes", kind="image", mime="image/png")
    assert a.path == b.path  # content-addressed → same file name
    assert a.id != b.id  # but two distinct records
    assert len(list(media_dir().glob("*.png"))) == 1  # one blob on disk


async def test_media_for_message_and_recent(store):
    m1 = await store.add_message("telegram", "in", "one")
    await store.add_media(b"a", kind="image", message_id=m1.id)
    await store.add_media(b"b", kind="audio", message_id=m1.id)
    await store.add_media(b"c", kind="document")  # unattached

    on_msg = await store.media_for_message(m1.id)
    assert {a.kind for a in on_msg} == {"image", "audio"}
    recent = await store.recent_media(limit=10)
    assert len(recent) == 3 and recent[0].kind == "document"  # newest first


async def test_media_passes_the_self_doctor(store):
    # A sourced artifact must not trip the provenance/FK integrity checks.
    from moko.store import integrity

    await store.add_media(b"x", kind="image", mime="image/png")
    findings = {f.name: f for f in await integrity.check(store.conn)}
    assert findings["Provenance"].ok and findings["References"].ok


# ── blob helpers ──────────────────────────────────────────────────────────────
def test_ext_for_prefers_filename_then_mime_then_bin():
    assert media_blobs.ext_for("image/jpeg", "cat.png") == ".png"  # the sender's own name wins
    assert media_blobs.ext_for("image/jpeg", None) == ".jpg"  # then the MIME type
    assert media_blobs.ext_for("audio/ogg", None) == ".ogg"
    assert media_blobs.ext_for(None, None) == ".bin"  # last resort


def test_write_blob_is_content_addressed(tmp_path, monkeypatch):
    rel, sha, size = media_blobs.write_blob(b"hello", mime="text/plain", filename="a.txt")
    assert rel == f"{sha}.txt" and size == 5
    assert (media_dir() / rel).read_bytes() == b"hello"
