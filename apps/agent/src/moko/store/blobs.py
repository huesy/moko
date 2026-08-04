"""Blob persistence for media artifacts.

The graph row (see `Store.add_media`) is the sourced record; the bytes live on
disk under `~/.moko/media/`, content-addressed by sha256 so identical files
(the same photo sent twice) share one blob. This module is the thin, sync seam
that writes and locates those bytes — kept out of the async store so the
loop-blocking linter stays honest about what touches the filesystem.
"""

from __future__ import annotations

import contextlib
import hashlib
import mimetypes
import os
from pathlib import Path

from ..config.paths import media_dir

# Telegram/voice MIME types that mimetypes doesn't always know, so a sane extension
# is still chosen rather than a bare ".bin".
_EXT_BY_MIME = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/gif": ".gif",
    "audio/ogg": ".ogg",
    "audio/mpeg": ".mp3",
    "audio/mp4": ".m4a",
    "video/mp4": ".mp4",
    "application/pdf": ".pdf",
}


def ext_for(mime: str | None, filename: str | None) -> str:
    """Pick a file extension from the filename first (it's the sender's own), then
    the MIME type, falling back to `.bin`."""
    if filename and "." in Path(filename).name:
        return Path(filename).suffix.lower()
    if mime:
        guess = _EXT_BY_MIME.get(mime) or mimetypes.guess_extension(mime.split(";")[0].strip())
        if guess:
            return guess
    return ".bin"


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def blob_path(rel_name: str) -> Path:
    """Absolute path of a stored blob from its relative name (what the DB holds)."""
    return media_dir() / rel_name


def write_blob(
    data: bytes, *, mime: str | None = None, filename: str | None = None
) -> tuple[str, str, int]:
    """Persist `data` content-addressed under the media dir. Returns
    `(relative_name, sha256, size)`. Idempotent: the same bytes overwrite the same
    file (identical content), so re-sending a file doesn't duplicate it on disk."""
    media_dir().mkdir(parents=True, exist_ok=True)
    sha = sha256_hex(data)
    rel_name = f"{sha}{ext_for(mime, filename)}"
    target = media_dir() / rel_name
    # Write via a temp file + atomic replace so a crash mid-write never leaves a
    # truncated blob the row claims is whole.
    staging = target.with_name(target.name + ".part")
    staging.write_bytes(data)
    os.replace(staging, target)
    with contextlib.suppress(OSError):
        os.chmod(target, 0o600)  # a private artifact, like the rest of the mind
    return rel_name, sha, len(data)
