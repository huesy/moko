"""Shared in-house full-screen TUI primitives — the keyboard-driven frame and
field editors used by both the first-run wizard (:mod:`moko.cli.wizard`) and the
live settings editor (:mod:`moko.cli.config_tui`).

Built on :mod:`rich`'s `Live` (alternate screen, flicker-free) and the
terminal-native key decoder in :mod:`moko.tui.keys` — no Textual, no new deps.
An editor handles keys and renders its own lines into the content pane:
``handle(key)`` returns ``None`` while editing, ``("commit", value)`` on confirm,
or ``("cancel", None)`` on abort; ``render(width)`` returns the lines to draw and
``hint`` the one-line key legend.
"""

from __future__ import annotations

import contextlib
import os
import select
import signal
import sys
import termios
import textwrap
import tty
from collections.abc import Callable
from typing import Any

from rich.text import Text

from ..theme import MOKO_PINK, MOKO_PINK_DIM
from ..tui import keys as K

ESCAPE = "escape"  # synthesized when a lone ESC arrives (keys.py only buffers it)
RESIZE = "resize"  # synthesized on SIGWINCH so the frame re-lays-out live
MUTED = "#8a8a8a"
DIVIDER = "#43354a"
# Pure white via truecolor (not ANSI "white", which many themes render as grey).
HILITE = f"bold #ffffff on {MOKO_PINK}"  # the highlighted row
CONTEXT = f"bold {MOKO_PINK}"  # the current category while focus is in the fields
WARN = "#ff9a7b"  # inline validation errors
GOOD = "#6fe09a"  # the "saved" / online confirmations
VIEWPORT = 12  # rows shown at once in a long, scrolling list

# vim-style movement, mapped to the arrow keys in list views.
_VIM = {"j": K.DOWN, "k": K.UP, "h": K.LEFT, "l": K.RIGHT}


def truncate(s: str, width: int) -> str:
    s = str(s)
    return s if len(s) <= width else s[: max(0, width - 1)] + "…"


def row(text: str, width: int, *, active: bool, base: str = "default") -> Text:
    """One option line, sized cell-aware so emoji/wide glyphs don't overflow.
    Active rows are padded (in the active style) so the pink runs edge to edge."""
    line = Text(f" {'›' if active else ' '} {text}", style=HILITE if active else base)
    line.truncate(width, overflow="ellipsis", pad=True)
    return line


def required(noun: str) -> Callable[[str], str | None]:
    return lambda v: None if v.strip() else f"{noun}, please :)"


def http_url(v: str) -> str | None:
    return None if v.strip().startswith("http") else "a URL starting with http, please"


# ── in-frame editors ─────────────────────────────────────────────────────────


class ListEditor:
    """A single-choice list, optionally type-to-filter, with a scrolling viewport.
    ↑/↓ move, ENTER picks, ESC cancels; when searchable, typing filters and
    Backspace edits the query (then cancels once it's empty)."""

    def __init__(self, title: str, choices: list[tuple[str, Any]], current: Any = None,
                 *, searchable: bool = False,
                 preview: Callable[[Any], str] | None = None) -> None:
        self.title = title
        self.choices = choices
        self.searchable = searchable
        self.preview = preview
        self.filter = ""
        self.idx = next((i for i, (_, v) in enumerate(choices) if v == current), 0)
        self._rows = VIEWPORT  # how many options to show at once; the frame may shrink it

    def set_viewport(self, rows: int) -> None:
        """Let the frame size the scrolling window to the space it actually has, so a
        long list never overflows (and a short terminal still gets a usable view)."""
        self._rows = max(3, rows)

    def _visible(self) -> list[tuple[str, Any]]:
        if not (self.searchable and self.filter):
            return self.choices
        needle = self.filter.lower()
        return [c for c in self.choices if needle in c[0].lower()]

    def handle(self, key: K.Key):
        vis = self._visible()
        # j/k move in a plain list; in a searchable one they're query characters.
        if not self.searchable and key.kind == K.TEXT and key.text in ("j", "k"):
            key = K.Key(_VIM[key.text])
        if key.kind == K.UP and vis:
            self.idx = (self.idx - 1) % len(vis)
        elif key.kind == K.DOWN and vis:
            self.idx = (self.idx + 1) % len(vis)
        elif key.kind == K.ENTER:
            if vis:
                return ("commit", vis[self.idx][1])
        elif key.kind == ESCAPE:
            return ("cancel", None)
        elif self.searchable and key.kind == K.TEXT:
            self.filter += key.text
            self.idx = 0
        elif key.kind == K.BACKSPACE:
            if self.searchable and self.filter:
                self.filter = self.filter[:-1]
                self.idx = 0
            else:
                return ("cancel", None)
        return None

    def render(self, width: int) -> list[Text]:
        if self.searchable:
            head = Text("  search: ", style=MUTED) + Text(self.filter or "…", style=MOKO_PINK_DIM)
        else:
            head = Text(f"  {self.title}", style=MUTED)
        lines = [head]
        vis = self._visible()
        # A live note about the highlighted choice (e.g. the local time in a zone).
        if self.preview and vis:
            note = self.preview(vis[self.idx][1])
            if note:
                lines.append(Text(f"  {note}", style=GOOD))
        lines.append(Text(""))
        if not vis:
            lines.append(Text("    (no matches)", style=MUTED))
            return lines
        start = max(0, min(self.idx - self._rows // 2, len(vis) - self._rows))
        window = vis[start:start + self._rows]
        if start > 0:
            lines.append(Text(f"    ↑ {start} more", style=MUTED))
        for i, (label, _) in enumerate(window, start):
            lines.append(row(label, width, active=i == self.idx))
        tail = len(vis) - (start + len(window))
        if tail > 0:
            lines.append(Text(f"    ↓ {tail} more", style=MUTED))
        return lines

    @property
    def hint(self) -> str:
        return ("type to filter · ↑↓ move · ⏎ choose · esc back" if self.searchable
                else "↑↓ move · ⏎ choose · esc back")


class CheckEditor:
    """A multi-select list. ↑/↓ move, SPACE toggles, ENTER confirms, ESC cancels.
    Choices may be ``(label, value)`` or ``(label, value, enabled)``; a disabled
    row is shown (e.g. a "coming soon" channel) but can't be toggled."""

    def __init__(self, title: str, choices: list[tuple], checked: list[Any]) -> None:
        self.title = title
        # Normalise to (label, value, enabled).
        self.choices = [(c[0], c[1], c[2] if len(c) > 2 else True) for c in choices]
        self.checked = {v for v in checked}
        self.idx = 0

    def _enabled(self, i: int) -> bool:
        return self.choices[i][2]

    def handle(self, key: K.Key):
        if key.kind == K.UP:
            self.idx = (self.idx - 1) % len(self.choices)
        elif key.kind == K.DOWN:
            self.idx = (self.idx + 1) % len(self.choices)
        elif key.kind == K.TEXT and key.text == " ":
            if self._enabled(self.idx):
                value = self.choices[self.idx][1]
                self.checked.discard(value) if value in self.checked else self.checked.add(value)
        elif key.kind == K.ENTER:
            return ("commit", [v for _, v, en in self.choices if en and v in self.checked])
        elif key.kind in (ESCAPE, K.BACKSPACE):
            return ("cancel", None)
        return None

    def render(self, width: int) -> list[Text]:
        lines = [Text(f"  {self.title}", style=MUTED), Text("")]
        for i, (label, value, enabled) in enumerate(self.choices):
            # · = locked (e.g. a "coming soon" channel), ● = picked, ○ = available
            marker = "·" if not enabled else ("●" if value in self.checked else "○")
            base = "default" if enabled else MUTED
            lines.append(row(f"{marker} {label}", width, active=i == self.idx, base=base))
        return lines

    hint = "↑↓ move · space tick · ⏎ continue · esc back"


class TextEditor:
    """A single-line text field. Type, Backspace deletes, ENTER saves, ESC cancels.
    `mask` hides the value (for API keys); `validate` returns an error string to
    block the save and show it inline, or None/"" to accept."""

    def __init__(self, title: str, value: str = "", *, mask: bool = False,
                 maxlen: int | None = None,
                 validate: Callable[[str], str | None] | None = None) -> None:
        self.title = title
        self.buf = value or ""
        self.mask = mask
        self.maxlen = maxlen
        self.validate = validate
        self.error = ""

    def handle(self, key: K.Key):
        if key.kind == K.TEXT:
            self.buf += key.text
            if self.maxlen is not None:  # cap length (also stops over-long pastes)
                self.buf = self.buf[: self.maxlen]
            self.error = ""
        elif key.kind == K.BACKSPACE:
            self.buf = self.buf[:-1]
            self.error = ""
        elif key.kind == K.ENTER:
            value = self.buf.strip()
            self.error = (self.validate(value) or "") if self.validate else ""
            if self.error:
                return None  # keep editing; the message shows below the field
            return ("commit", value)
        elif key.kind == ESCAPE:
            return ("cancel", None)
        return None

    def render(self, width: int) -> list[Text]:
        shown = "●" * len(self.buf) if self.mask else self.buf
        lines = [
            Text(f"  {self.title}", style=MUTED),
            Text(""),
            Text(f"  {truncate(shown, width - 4)}", style=f"bold {MOKO_PINK}")
            + Text("▌", style=MOKO_PINK_DIM),
        ]
        if self.error:
            lines += [Text(""), Text(f"  {truncate(self.error, width - 4)}", style=WARN)]
        return lines

    hint = "type · ⏎ continue · esc back"


class InfoEditor:
    """A read-only panel of lines — confirmation/next-steps shown in-frame. Any of
    ENTER/ESC closes it."""

    def __init__(self, title: str, lines: list[str]) -> None:
        self.title = title
        self.lines = lines

    def handle(self, key: K.Key):
        if key.kind in (K.ENTER, ESCAPE, K.BACKSPACE):
            return ("commit", None)
        return None

    def render(self, width: int) -> list[Text]:
        out = [Text(f"  {self.title}", style=CONTEXT), Text("")]
        for ln in self.lines:
            out.append(Text(f"  {truncate(ln, width - 4)}",
                            style=MUTED if ln.startswith(" ") else "default"))
        return out

    hint = "⏎ done"


class ScrollEditor:
    """A read-only, word-wrapped, scrollable body of text — for a consent screen.
    ↑/↓ (and PageUp/Down via Home/End) scroll; ENTER accepts (``("commit", True)``),
    ESC declines (``("cancel", None)``). The frame calls :meth:`set_viewport` with
    the number of body rows that fit before each render."""

    def __init__(self, title: str, body: str) -> None:
        self.title = title
        self.paragraphs = [p.strip() for p in body.split("\n\n")]
        self.top = 0
        self._rows = 14
        self._wrapped: list[str] = []
        self._wrap_width = 0

    def set_viewport(self, rows: int) -> None:
        self._rows = max(3, rows)

    def _lines(self, width: int) -> list[str]:
        if width != self._wrap_width:  # re-wrap only when the width changes
            self._wrap_width = width
            out: list[str] = []
            for i, para in enumerate(self.paragraphs):
                if i:
                    out.append("")
                out.extend(textwrap.wrap(para, width) or [""])
            self._wrapped = out
        return self._wrapped

    def handle(self, key: K.Key):
        if key.kind == K.UP:
            self.top = max(0, self.top - 1)
        elif key.kind == K.DOWN:
            self.top += 1  # clamped against the body length at render time
        elif key.kind == K.HOME:
            self.top = 0
        elif key.kind == K.END:
            self.top = 10 ** 9
        elif key.kind == K.ENTER:
            return ("commit", True)
        elif key.kind in (ESCAPE, K.BACKSPACE):
            return ("cancel", None)
        return None

    def render(self, width: int) -> list[Text]:
        body = self._lines(max(8, width - 2))
        rows = self._rows
        max_top = max(0, len(body) - rows)
        self.top = min(self.top, max_top)
        window = body[self.top:self.top + rows]
        out = [Text(f"  {self.title}", style=CONTEXT), Text("")]
        for ln in window:
            out.append(Text(f"  {ln}", style="default"))
        # Pad so the accept/decline footer sits at a stable spot.
        out += [Text("") for _ in range(max(0, rows - len(window)))]
        more_up = "↑ more" if self.top > 0 else ""
        more_dn = "↓ more" if self.top < max_top else ""
        gauge = f"  {more_up}{'   ' if more_up and more_dn else ''}{more_dn}".rstrip()
        out.append(Text(gauge or "  ", style=MUTED))
        return out

    hint = "↑↓ scroll · ⏎ accept · esc not now"


# ── terminal session ─────────────────────────────────────────────────────────


class Terminal:
    """cbreak input with signals/flow-control off (so Ctrl+C and Enter arrive as
    bytes) and the kitty key protocol on. A self-pipe fed by SIGWINCH lets a
    blocking read wake on a terminal resize, so the frame re-lays-out live."""

    def __init__(self) -> None:
        self.fd = sys.stdin.fileno()
        self._decoder = K.KeyDecoder()
        self._saved = termios.tcgetattr(self.fd)

    def __enter__(self) -> Terminal:
        tty.setcbreak(self.fd)
        attrs = termios.tcgetattr(self.fd)
        attrs[3] &= ~termios.ISIG  # Ctrl+C as a byte, not a signal
        attrs[0] &= ~termios.IXON  # Ctrl+S/Q as bytes
        attrs[0] &= ~termios.ICRNL  # keep Enter as CR, distinct from Ctrl+J
        termios.tcsetattr(self.fd, termios.TCSADRAIN, attrs)
        sys.stdout.write(K.KITTY_ON)
        sys.stdout.flush()
        # SIGWINCH → a byte on the self-pipe, which select() below watches.
        self._winch_r, self._winch_w = os.pipe()
        os.set_blocking(self._winch_r, False)
        os.set_blocking(self._winch_w, False)
        self._old_winch = signal.signal(signal.SIGWINCH, lambda *_: None)
        self._old_wakeup = signal.set_wakeup_fd(self._winch_w, warn_on_full_buffer=False)
        return self

    def __exit__(self, *exc) -> None:
        signal.set_wakeup_fd(self._old_wakeup)
        signal.signal(signal.SIGWINCH, self._old_winch)
        os.close(self._winch_r)
        os.close(self._winch_w)
        sys.stdout.write(K.KITTY_OFF)
        sys.stdout.flush()
        termios.tcsetattr(self.fd, termios.TCSADRAIN, self._saved)

    def read_key(self) -> K.Key:
        """Block for the next key. A terminal resize returns RESIZE (so the caller
        re-renders); a lone ESC byte with nothing following within a beat is
        ESCAPE; otherwise it's the start of a CSI sequence the decoder assembles."""
        while True:
            ready, _, _ = select.select([self.fd, self._winch_r], [], [])
            if self._winch_r in ready:
                with contextlib.suppress(OSError):
                    os.read(self._winch_r, 64)  # drain the signal byte(s)
                if self.fd not in ready:
                    return K.Key(RESIZE)
            data = os.read(self.fd, 4096)
            if data == b"\x1b" and not self._decoder._buf:
                more, _, _ = select.select([self.fd], [], [], 0.04)
                if not more:
                    return K.Key(ESCAPE)
            for key in self._decoder.feed(data):
                return key
