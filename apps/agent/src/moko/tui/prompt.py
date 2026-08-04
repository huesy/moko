"""The composer: a multi-line input and a status bar that render in place at
the current cursor line, with `emit()` printing messages above them into the
terminal's native scrollback.

It follows the terminal's natural flow — no screen-bottom anchoring. The region
is redrawn relative to where the cursor already is (move up to its top, clear,
redraw), so it's immune to the scrolling that scrollback output causes. Adding
lines grows it downward until it reaches the bottom, where the terminal scrolls
older lines into history (the "expand upward" feel); deleting lines pulls the
status bar back up. Just like a normal prompt.

Terminal mode is cbreak with signals off (not full raw), so the kernel still
translates newlines on output — keeping rich's `emit()` and our rendered lines
clean — while we read keys one at a time and see Ctrl+C as a byte.
"""

from __future__ import annotations

import asyncio
import os
import re
import shutil
import sys
import termios
import tty

from ..theme import console
from . import keys as K
from .editor import Editor, wrapped_rows

_ESC = "\x1b"
_SGR = re.compile(r"\x1b\[[0-9;]*m")


def paint(text: str, hex_color: str) -> str:
    h = hex_color.lstrip("#")
    r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    return f"{_ESC}[38;2;{r};{g};{b}m{text}{_ESC}[0m"


def dim(text: str) -> str:
    return f"{_ESC}[2m{text}{_ESC}[0m"


def _visible_len(s: str) -> int:
    return len(_SGR.sub("", s))


class Prompt:
    def __init__(self, *, prompt: str = "› ", prompt_color: str = "#ff6fae") -> None:
        self._prompt = prompt
        self._prompt_color = prompt_color
        self._status = ""
        self._pending = ""  # a transient line above the input (e.g. thinking…)
        self._editor = Editor()
        self._decoder = K.KeyDecoder()
        self._fd = sys.stdin.fileno()
        self._saved: list | None = None
        self._cursor_row = 0  # cursor's row within the region (from its top)
        self._submissions: asyncio.Queue[str | None] = asyncio.Queue()

    # ── lifecycle ─────────────────────────────────────────────────────
    async def __aenter__(self) -> Prompt:
        self._saved = termios.tcgetattr(self._fd)
        tty.setcbreak(self._fd)
        attrs = termios.tcgetattr(self._fd)
        attrs[3] &= ~termios.ISIG  # Ctrl+C/Z as bytes
        attrs[0] &= ~termios.IXON  # Ctrl+S/Q as bytes
        attrs[0] &= ~termios.ICRNL  # keep CR (Enter) and LF (Ctrl+J) distinct
        termios.tcsetattr(self._fd, termios.TCSADRAIN, attrs)
        sys.stdout.write(K.KITTY_ON)
        sys.stdout.flush()
        asyncio.get_running_loop().add_reader(self._fd, self._on_readable)
        self._draw()
        return self

    async def __aexit__(self, *exc) -> None:
        asyncio.get_running_loop().remove_reader(self._fd)
        self._erase()
        sys.stdout.write(K.KITTY_OFF)
        sys.stdout.flush()
        if self._saved is not None:
            termios.tcsetattr(self._fd, termios.TCSADRAIN, self._saved)

    # ── public surface ────────────────────────────────────────────────
    async def read(self) -> str | None:
        """The next submitted message, or None on Ctrl+C / Ctrl+D."""
        return await self._submissions.get()

    def set_status(self, status: str) -> None:
        self._status = status
        self._draw()

    def set_pending(self, pending: str) -> None:
        self._pending = pending
        self._draw()

    def emit(self, renderable) -> None:
        """Print above the composer, into the terminal's native scrollback."""
        self._erase()
        console.print(renderable)
        self._draw()

    # ── input ─────────────────────────────────────────────────────────
    def _on_readable(self) -> None:
        try:
            data = os.read(self._fd, 4096)
        except OSError:
            return
        if not data:
            self._submissions.put_nowait(None)
            return
        for key in self._decoder.feed(data):
            self._handle(key)

    def _handle(self, key: K.Key) -> None:
        ed = self._editor
        kind = key.kind
        if kind in (K.INTERRUPT, K.EOF):
            self._submissions.put_nowait(None)
            return
        if kind == K.ENTER:
            if ed.wants_continue:
                ed.continue_line()
            else:
                text = ed.text
                ed.clear()
                self._draw()
                self._submissions.put_nowait(text)
                return
        elif kind == K.NEWLINE:
            ed.newline()
        elif kind == K.TEXT:
            ed.insert(key.text)
        elif kind == K.BACKSPACE:
            ed.backspace()
        elif kind == K.DELETE:
            ed.delete()
        elif kind == K.LEFT:
            ed.left()
        elif kind == K.RIGHT:
            ed.right()
        elif kind == K.HOME:
            ed.home()
        elif kind == K.END:
            ed.end()
        else:
            return
        self._draw()

    # ── rendering ─────────────────────────────────────────────────────
    def _width(self) -> int:
        return shutil.get_terminal_size((80, 24)).columns

    def _region(self) -> tuple[list[str], list[int]]:
        """The region's display lines (ANSI) and their visible widths: an
        optional pending line, the input (prompt on line 0, the rest flush left
        so it matches the submitted message), then the status bar."""
        lines, widths = [], []
        if self._pending:
            lines.append(self._pending)
            widths.append(_visible_len(self._pending))
        for i, line in enumerate(self._editor.lines):
            if i == 0:
                lines.append(paint(self._prompt, self._prompt_color) + line)
                widths.append(len(self._prompt) + len(line))
            else:
                lines.append(line)
                widths.append(len(line))
        lines.append(self._status)
        widths.append(_visible_len(self._status))
        return lines, widths

    def _cursor_rc(self, width: int) -> tuple[int, int]:
        """The cursor's (row within the region, column), wrap-aware."""
        pending_rows = wrapped_rows(width, _visible_len(self._pending)) if self._pending else 0
        widths = [
            (len(self._prompt) if i == 0 else 0) + len(line)
            for i, line in enumerate(self._editor.lines)
        ]
        line_idx, col = self._editor.cursor_line_col
        flat = (len(self._prompt) if line_idx == 0 else 0) + col
        crow = pending_rows + wrapped_rows(width, *widths[:line_idx]) + flat // width
        return crow, flat % width

    def _erase(self) -> None:
        out = ""
        if self._cursor_row:
            out += f"{_ESC}[{self._cursor_row}A"
        out += f"\r{_ESC}[J"
        sys.stdout.write(out)
        sys.stdout.flush()
        self._cursor_row = 0

    def _draw(self) -> None:
        width = self._width()
        lines, widths = self._region()
        out = ""
        if self._cursor_row:  # back up to the top of the current region
            out += f"{_ESC}[{self._cursor_row}A"
        out += f"\r{_ESC}[J"  # column 0, clear the old region (and below it)
        out += "\n".join(lines)  # redraw; overflow at the bottom scrolls naturally
        crow, ccol = self._cursor_rc(width)
        up = (wrapped_rows(width, *widths) - 1) - crow
        out += "\r"
        if up > 0:
            out += f"{_ESC}[{up}A"
        if ccol > 0:
            out += f"{_ESC}[{ccol}C"
        sys.stdout.write(out)
        sys.stdout.flush()
        self._cursor_row = crow
