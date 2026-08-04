"""EXIF strip (privacy) + read (time-awareness).

The load-bearing guarantee: an outbound image leaks NO GPS. Also that reading is
correct (decimal lat/lon incl. S/W sign, capture time), and — critically — that
non-JPEG / corrupt / path-looking bytes never reach piexif (which would treat them
as a filename and open() them: an arbitrary-file-read footgun).
"""

import base64
import io

import piexif

from moko.store import exif

# A tiny (225-byte) real baseline JPEG, so the test is hermetic.
_TINY_JPEG = base64.b64decode(
    "/9j/4AAQSkZJRgABAgAAAQABAAD//gAQTGF2YzYwLjMxLjEwMgD/2wBDAAgEBAQEBAUFBQUFBQYG"
    "BgYGBgYGBgYGBgYHBwcICAgHBwcGBgcHCAgICAkJCQgICAgJCQoKCgwMCwsODg4RERT/xABMAAEB"
    "AAAAAAAAAAAAAAAAAAAABwEBAQAAAAAAAAAAAAAAAAAABQcQAQAAAAAAAAAAAAAAAAAAAAARAQAA"
    "AAAAAAAAAAAAAAAAAAD/wAARCAAIAAgDASIAAhEAAxEA/9oADAMBAAIRAxEAPwCOAL+Lf//Z"
)


def _deg(d, m, s):
    return ((d, 1), (m, 1), (int(s * 100), 100))


def _jpeg_with_gps(*, lat_ref=b"S", lon_ref=b"E", offset=None) -> bytes:
    exif_d = {
        "0th": {piexif.ImageIFD.DateTime: b"2026:06:20 14:30:00"},
        "Exif": {piexif.ExifIFD.DateTimeOriginal: b"2026:06:20 14:30:00"},
        "GPS": {
            piexif.GPSIFD.GPSLatitudeRef: lat_ref, piexif.GPSIFD.GPSLatitude: _deg(35, 0, 42.0),
            piexif.GPSIFD.GPSLongitudeRef: lon_ref, piexif.GPSIFD.GPSLongitude: _deg(135, 46, 5.0),
        },
        "1st": {},
    }
    if offset:
        exif_d["Exif"][piexif.ExifIFD.OffsetTimeOriginal] = offset
    out = io.BytesIO()
    piexif.insert(piexif.dump(exif_d), _TINY_JPEG, out)
    return out.getvalue()


def test_strip_removes_all_gps_and_exif():
    jpg = _jpeg_with_gps()
    assert exif.read_exif(jpg, "image/jpeg")  # GPS is there to begin with
    stripped = exif.strip_exif(jpg, "image/jpeg")
    # Nothing left to read, and it's still a valid JPEG.
    assert exif.read_exif(stripped, "image/jpeg") == {}
    assert stripped[:3] == b"\xff\xd8\xff" and len(stripped) < len(jpg)


def test_read_gps_decimal_and_capture_time():
    meta = exif.read_exif(_jpeg_with_gps(), "image/jpeg")
    assert meta["lat"] == -35.011667 and meta["lon"] == 135.768056  # S → negative
    assert meta["taken_at"] == "2026-06-20T14:30:00"  # tz-naive, not faked to UTC


def test_north_west_signs():
    meta = exif.read_exif(_jpeg_with_gps(lat_ref=b"N", lon_ref=b"W"), "image/jpeg")
    assert meta["lat"] > 0 and meta["lon"] < 0


def test_capture_time_honors_offset_when_present():
    meta = exif.read_exif(_jpeg_with_gps(offset=b"+09:00"), "image/jpeg")
    assert meta["taken_at"] == "2026-06-20T14:30:00+09:00"


def test_strip_is_a_noop_for_png_and_unknown():
    png = b"\x89PNG\r\n\x1a\nthe rest is not jpeg"
    assert exif.strip_exif(png, "image/png") == png
    assert exif.strip_exif(png, "image/jpeg") == png  # mislabeled mime → still gated by magic
    assert exif.read_exif(png, "image/png") == {}


def test_path_looking_bytes_never_reach_piexif():
    # The footgun: piexif open()s non-JPEG bytes as a filename. The magic-byte gate
    # must stop that — even when the mime claims jpeg.
    assert exif.read_exif(b"/etc/passwd\x00", "image/jpeg") == {}
    assert exif.strip_exif(b"/etc/passwd\x00", "image/jpeg") == b"/etc/passwd\x00"
    assert exif.read_exif(b"", "image/jpeg") == {}


def test_jpeg_without_exif_reads_empty_and_strips_cleanly():
    assert exif.read_exif(_TINY_JPEG, "image/jpeg") == {}
    assert exif.strip_exif(_TINY_JPEG, "image/jpeg")[:3] == b"\xff\xd8\xff"


# ── integration: the real send/receive paths ──────────────────────────────────
import pytest  # noqa: E402

from moko.agent import Brain  # noqa: E402
from moko.config.config import Config  # noqa: E402
from moko.config.paths import ensure_home, media_dir  # noqa: E402
from moko.llm.base import LLMResponse  # noqa: E402
from moko.store import Store  # noqa: E402

_CFG = Config(companion_name="A", timezone="Asia/Tokyo", sleep_start="23:00",
              sleep_end="07:00", proactivity="moderate")


@pytest.fixture
async def store(tmp_path):
    s = await Store.open(tmp_path / "moko.db")
    try:
        yield s
    finally:
        await s.close()


class _VisionLLM:
    model = "fake"
    supports_vision = True

    def __init__(self):
        self.last = None

    async def converse(self, system, messages, *, tools, execute, max_tokens=1024,
                       temperature=1.0, max_steps=6, **kw):
        self.last = messages[-1].content
        return LLMResponse("nice photo")


async def test_inbound_image_stores_capture_time_and_surfaces_it(store):
    from moko.channels.base import Attachment

    llm = _VisionLLM()
    att = Attachment(kind="image", data=_jpeg_with_gps(offset=b"+09:00"),
                     mime="image/jpeg", filename="trip.jpg", caption=None, ref="f")
    await Brain(store, llm, _CFG).respond("here", attachments=[att])

    art = (await store.recent_media())[0]
    assert art.taken_at == "2026-06-20T14:30:00+09:00"  # capture time kept (time-aware)
    # The turn surfaced the capture time + GPS so Moko could speak to it.
    last = llm.last
    blob = "".join(p for p in last if isinstance(p, str)) if isinstance(last, list) else last
    assert "Image details" in blob and "2026-06-20" in blob


def _png_with_exif() -> bytes:
    import struct
    import zlib

    def chunk(t: bytes, d: bytes) -> bytes:
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)

    return b"\x89PNG\r\n\x1a\n" + chunk(b"eXIf", b"some-exif") + chunk(b"IEND", b"")


def test_has_exif_is_a_fail_closed_verifier():
    assert exif.has_exif(_jpeg_with_gps(), "image/jpeg") is True
    assert exif.has_exif(exif.strip_exif(_jpeg_with_gps(), "image/jpeg"), "image/jpeg") is False
    assert exif.has_exif(_png_with_exif(), "image/png") is True  # PNG eXIf chunk caught
    assert exif.has_exif(b"\x89PNG\r\n\x1a\n", "image/png") is False  # clean PNG
    # A JPEG-magic'd buffer piexif can't parse → treated as unsafe (has metadata).
    assert exif.has_exif(b"\xff\xd8\xff garbage that won't parse", "image/jpeg") is True


async def test_send_refuses_when_metadata_survives_strip(store, monkeypatch):
    # Simulate strip failing (fail-open): the send path must REFUSE, not leak.
    ensure_home()
    f = media_dir() / "leaky.jpg"
    f.write_bytes(_jpeg_with_gps())
    from moko.agent import brain as brain_mod
    monkeypatch.setattr(brain_mod.exif, "strip_exif", lambda data, mime=None: data)  # no-op strip

    sent = {}

    async def send_file(*, data, filename, caption, kind):
        sent["data"] = data

    b = Brain(store, _VisionLLM(), _CFG)
    b._send_file = send_file
    b._reply_channel = "telegram"
    out = await b._send_attachment({"path": str(f)})
    assert "won't send" in out and not sent  # refused; nothing left the machine


async def test_outbound_send_strips_exif(store):
    ensure_home()
    f = media_dir() / "shared.jpg"
    f.write_bytes(_jpeg_with_gps())  # a photo with GPS, sitting in the media folder
    assert exif.read_exif(f.read_bytes(), "image/jpeg")  # has GPS before sending

    sent = {}

    async def send_file(*, data, filename, caption, kind):
        sent["data"] = data

    brain = Brain(store, _VisionLLM(), _CFG)
    brain._send_file = send_file
    brain._reply_channel = "telegram"
    out = await brain._send_attachment({"path": str(f)})
    assert "Sent shared.jpg" in out
    # The bytes that left the machine carry NO GPS.
    assert exif.read_exif(sent["data"], "image/jpeg") == {}
