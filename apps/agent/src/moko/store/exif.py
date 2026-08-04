"""EXIF handling for image bytes — strip on the way out, read on the way in.

Two jobs, both privacy-shaped:

  • **strip_exif** — before Moko sends an image, remove its EXIF (GPS coordinates,
    camera serial, capture time, the embedded thumbnail's metadata). A photo's
    EXIF GPS silently leaks where the companion was; sending it off-box is a
    floor-3 ("data stays on the machine") concern, so we scrub it. Verified: a
    `piexif.remove` clears the whole APP1/EXIF segment, GPS included.
  • **read_exif** — when Moko receives an image, read its GPS + capture time, so
    it knows *where/when* a photo was taken (it's deeply time-aware). This is the
    companion's own data on their own machine.

Scoped to JPEG (where EXIF/GPS actually lives — phone photos, and the Telegram
*document* path that preserves originals). Non-JPEG and anything unparseable
fail **safe**: strip returns the bytes unchanged (PNG/WebP rarely carry GPS, and
Moko's generated PNGs carry none), read returns nothing. piexif raises on
non-JPEG input (it mistakes the bytes for a path → FileNotFoundError), so every
call is guarded.
"""

from __future__ import annotations

import datetime as dt
import io

import piexif

from ..log import get_logger

_log = get_logger("exif")

_JPEG_MIMES = {"image/jpeg", "image/jpg", "image/pjpeg"}


def _is_jpeg(data: bytes, mime: str | None) -> bool:
    """An AIRTIGHT gate before any piexif call. piexif treats bytes that aren't a
    JPEG/WebP as a FILE PATH and calls open() on them (an arbitrary-file-read footgun)
    and raises on PNG/WebP/corrupt — so we must REQUIRE the JPEG SOI magic, never trust
    the mime alone. An explicitly non-JPEG mime is rejected outright; otherwise the
    bytes themselves must begin with the magic."""
    if mime is not None and mime.lower() not in _JPEG_MIMES:
        return False
    return len(data) >= 3 and data[:3] == b"\xff\xd8\xff"


_PNG_SIG = b"\x89PNG\r\n\x1a\n"


def _is_png(data: bytes) -> bool:
    return data[:8] == _PNG_SIG


def _png_has_exif(data: bytes) -> bool:
    """True if a PNG carries an `eXIf` chunk (where PNG GPS/metadata lives). Walks the
    chunk list defensively; a malformed stream is treated as 'has metadata' (unsafe)."""
    pos = 8
    while pos + 12 <= len(data):
        length = int.from_bytes(data[pos:pos + 4], "big")
        ctype = data[pos + 4:pos + 8]
        if ctype == b"eXIf":
            return True
        if ctype == b"IEND":
            return False
        nxt = pos + 12 + length
        if nxt <= pos or nxt > len(data):
            return True  # can't parse cleanly → don't claim it's metadata-free
        pos = nxt
    return False


def strip_exif(data: bytes, mime: str | None = None) -> bytes:
    """Return `data` with its EXIF removed (JPEG); for anything else, or on any
    failure, return it unchanged. Best-effort — the caller MUST verify the result with
    `has_exif` and refuse to send if metadata survives (fail-closed). A JPEG we couldn't
    scrub is logged, because it means metadata might ride along."""
    if not _is_jpeg(data, mime):
        return data
    try:
        out = io.BytesIO()
        piexif.remove(data, out)
        return out.getvalue()
    except Exception:
        _log.warning("couldn't strip EXIF from a %s image", mime, exc_info=True)
        return data


def has_exif(data: bytes, mime: str | None = None) -> bool:
    """Does this image still carry EXIF/metadata? The privacy verifier the send path
    uses AFTER stripping: if this is True, the strip didn't take and the image must not
    leave the machine. Fail-closed — a JPEG we can't parse counts as 'has metadata'."""
    if _is_jpeg(data, mime):
        try:
            tags = piexif.load(data)
        except Exception:
            return True  # unparseable → can't prove it's clean → treat as unsafe
        if tags.get("thumbnail"):
            return True
        return any(tags.get(ifd) for ifd in ("0th", "Exif", "GPS", "1st"))
    if _is_png(data):
        return _png_has_exif(data)
    return False


def _to_decimal(rational, ref: bytes) -> float | None:
    """Convert EXIF GPS (degrees, minutes, seconds as rationals) + a hemisphere ref
    to a signed decimal degree. S and W are negative."""
    try:
        d = rational[0][0] / rational[0][1]
        m = rational[1][0] / rational[1][1]
        s = rational[2][0] / rational[2][1]
        value = d + m / 60 + s / 3600
    except (TypeError, IndexError, ZeroDivisionError):
        return None
    if isinstance(ref, bytes) and ref.upper() in (b"S", b"W"):
        value = -value
    return round(value, 6)


def _parse_dt(raw, offset=None) -> str | None:
    """EXIF datetime is 'YYYY:MM:DD HH:MM:SS', tz-naive (the camera's wall clock).
    Baseline EXIF carries no timezone, so we do NOT fabricate UTC — we keep the naive
    ISO string (it's the capture time, distinct from when Moko received it). When EXIF
    2.31 `OffsetTimeOriginal` ('+09:00') is present, we apply it for a real aware time."""
    if not raw:
        return None
    try:
        text = raw.decode("ascii", "ignore") if isinstance(raw, bytes) else str(raw)
        parsed = dt.datetime.strptime(text.replace("\x00", "").strip(), "%Y:%m:%d %H:%M:%S")
    except (ValueError, AttributeError):
        return None
    if offset:
        try:
            off = offset.decode("ascii", "ignore") if isinstance(offset, bytes) else str(offset)
            off = off.replace("\x00", "").strip()
            sign = -1 if off[0] == "-" else 1
            hh, mm = off[1:].split(":")
            tz = dt.timezone(sign * dt.timedelta(hours=int(hh), minutes=int(mm)))
            return parsed.replace(tzinfo=tz).isoformat()
        except (ValueError, IndexError, AttributeError):
            pass
    return parsed.isoformat()


def read_exif(data: bytes, mime: str | None = None) -> dict:
    """Read the interesting EXIF from an image: GPS as decimal lat/lon, and the
    capture time. Returns a dict with only the keys it found (``lat``, ``lon``,
    ``taken_at``); empty on a non-JPEG, no-EXIF, or unparseable image."""
    if not _is_jpeg(data, mime):
        return {}
    try:
        tags = piexif.load(data)
    except Exception:
        return {}
    out: dict = {}
    gps = tags.get("GPS") or {}
    lat = gps.get(piexif.GPSIFD.GPSLatitude)
    lon = gps.get(piexif.GPSIFD.GPSLongitude)
    if lat and lon:
        dlat = _to_decimal(lat, gps.get(piexif.GPSIFD.GPSLatitudeRef, b"N"))
        dlon = _to_decimal(lon, gps.get(piexif.GPSIFD.GPSLongitudeRef, b"E"))
        if dlat is not None and dlon is not None:
            out["lat"], out["lon"] = dlat, dlon
    exif_ifd = tags.get("Exif") or {}
    taken = exif_ifd.get(piexif.ExifIFD.DateTimeOriginal)
    taken = taken or (tags.get("0th") or {}).get(piexif.ImageIFD.DateTime)
    when = _parse_dt(taken, exif_ifd.get(piexif.ExifIFD.OffsetTimeOriginal))
    if when:
        out["taken_at"] = when
    return out
