"""Decode raw terminal bytes into `Key` events.

Handles the legacy encodings every terminal sends *and* the kitty keyboard
protocol's parametric `CSI code ; modifiers : event u` codes — masking the
NumLock/CapsLock bits out of the modifier, which is what made a static lookup
table impossible. Enabling the protocol (see `KITTY_ON`) is what lets us tell
Shift+Enter apart from Enter; on terminals that don't support it (e.g. macOS
Terminal.app) those codes never arrive, so Shift+Enter is indistinguishable
from Enter — which is why Ctrl+J (LF) is the universal "insert newline" key:
it arrives as a distinct byte everywhere, given `ICRNL` is off so the Enter
key's CR isn't folded into LF before we see it.

The decoder buffers partial escape sequences across `feed()` calls, so a
sequence split over two reads is still parsed correctly.
"""

from __future__ import annotations

from dataclasses import dataclass

# Push/pop the kitty "disambiguate escape codes" flag: modified keys then arrive
# as CSI-u codes. Pop on exit so the terminal's keyboard mode is left untouched.
KITTY_ON = "\x1b[>1u"
KITTY_OFF = "\x1b[<u"

# Key kinds. `text` carries the typed characters; the rest are bare signals.
TEXT = "text"
ENTER = "enter"  # submit
NEWLINE = "newline"  # insert a line break (Ctrl+J everywhere; Shift+Enter on kitty)
BACKSPACE = "backspace"
DELETE = "delete"
LEFT = "left"
RIGHT = "right"
UP = "up"
DOWN = "down"
HOME = "home"
END = "end"
INTERRUPT = "interrupt"  # Ctrl+C
EOF = "eof"  # Ctrl+D


@dataclass
class Key:
    kind: str
    text: str = ""


_LOCK_BITS = 64 | 128  # CapsLock | NumLock — not real modifiers; mask them off


class KeyDecoder:
    def __init__(self) -> None:
        self._buf = b""

    def feed(self, data: bytes) -> list[Key]:
        """Add bytes and return whatever complete keys can now be parsed."""
        self._buf += data
        out: list[Key] = []
        while self._buf:
            key, consumed = self._parse(self._buf)
            if consumed == 0:
                break  # incomplete sequence — wait for more bytes
            if key is not None:
                out.append(key)
            self._buf = self._buf[consumed:]
        return out

    def _parse(self, buf: bytes) -> tuple[Key | None, int]:
        b0 = buf[0]
        if b0 == 0x1B:
            return self._escape(buf)
        if b0 == 0x0D:  # CR — the Enter key (ICRNL is off, so it stays CR)
            return Key(ENTER), 1
        if b0 == 0x0A:  # LF — Ctrl+J, our universal "insert newline" key
            return Key(NEWLINE), 1
        if b0 in (0x7F, 0x08):
            return Key(BACKSPACE), 1
        if b0 == 0x03:
            return Key(INTERRUPT), 1
        if b0 == 0x04:
            return Key(EOF), 1
        if b0 < 0x20:
            return None, 1  # other control bytes: ignore
        return self._text(buf)

    def _text(self, buf: bytes) -> tuple[Key | None, int]:
        i = 0
        while i < len(buf) and buf[i] >= 0x20 and buf[i] != 0x7F:
            i += 1
        chunk = buf[:i]
        try:
            return Key(TEXT, chunk.decode("utf-8")), i
        except UnicodeDecodeError as exc:
            if exc.start == 0:
                return None, 0  # truncated multi-byte char at the front — wait
            return Key(TEXT, chunk[: exc.start].decode("utf-8")), exc.start

    def _escape(self, buf: bytes) -> tuple[Key | None, int]:
        if len(buf) == 1:
            return None, 0  # lone ESC, or the start of a sequence — wait
        if buf[1] != 0x5B:  # not a CSI "[" — alt-modified key or bare ESC; drop ESC
            return None, 1
        i = 2
        while i < len(buf) and 0x30 <= buf[i] <= 0x3F:  # parameter bytes
            i += 1
        if i >= len(buf):
            return None, 0  # final byte not arrived yet
        params = buf[2:i].decode("ascii", "ignore")
        return self._csi(chr(buf[i]), params), i + 1

    def _csi(self, final: str, params: str) -> Key | None:
        if final == "u":
            return self._kitty(params)
        if final == "~":
            parts = params.split(";")
            if params == "3":
                return Key(DELETE)
            # xterm modifyOtherKeys form for Enter: CSI 27 ; mod ; 13 ~
            if len(parts) == 3 and parts[0] == "27" and parts[2] == "13":
                return self._from_code(13, *_mods(parts[1]))
            return None
        return {
            "A": Key(UP),
            "B": Key(DOWN),
            "C": Key(RIGHT),
            "D": Key(LEFT),
            "H": Key(HOME),
            "F": Key(END),
        }.get(final)

    def _kitty(self, params: str) -> Key | None:
        parts = params.split(";")
        try:
            code = int(parts[0])
        except ValueError:
            return None
        mod_field = parts[1] if len(parts) > 1 else "1"
        shift, ctrl, released = _mods(mod_field)
        if released:
            return None  # ignore key-release events
        return self._from_code(code, shift, ctrl)

    def _from_code(self, code: int, shift: bool, ctrl: bool) -> Key | None:
        if code == 13:
            return Key(NEWLINE) if shift else Key(ENTER)
        if code in (127, 8):
            return Key(BACKSPACE)
        if ctrl and code == ord("c"):
            return Key(INTERRUPT)
        if ctrl and code == ord("d"):
            return Key(EOF)
        return None  # other modified keys aren't needed by the composer


def _mods(field: str) -> tuple[bool, bool, bool]:
    """Decode a kitty modifier field "mod[:event]" → (shift, ctrl, released)."""
    head, _, event = field.partition(":")
    try:
        bits = (int(head) - 1) & ~_LOCK_BITS
    except ValueError:
        bits = 0
    released = event == "3"
    return bool(bits & 1), bool(bits & 4), released
