"""A pure multi-line edit buffer: text plus a cursor, and the geometry needed
to render it. No terminal I/O — that lives in prompt.py, so this is fully
unit-testable.
"""

from __future__ import annotations

import math


class Editor:
    def __init__(self) -> None:
        self.text = ""
        self.cursor = 0

    def insert(self, s: str) -> None:
        self.text = self.text[: self.cursor] + s + self.text[self.cursor :]
        self.cursor += len(s)

    def newline(self) -> None:
        self.insert("\n")

    def continue_line(self) -> None:
        """Turn a trailing backslash into a newline (the `\\`-then-Enter idiom)."""
        if self.text.endswith("\\"):
            self.text = self.text[:-1] + "\n"
            self.cursor = len(self.text)

    @property
    def wants_continue(self) -> bool:
        return self.text.endswith("\\")

    def backspace(self) -> None:
        if self.cursor > 0:
            self.text = self.text[: self.cursor - 1] + self.text[self.cursor :]
            self.cursor -= 1

    def delete(self) -> None:
        if self.cursor < len(self.text):
            self.text = self.text[: self.cursor] + self.text[self.cursor + 1 :]

    def left(self) -> None:
        self.cursor = max(0, self.cursor - 1)

    def right(self) -> None:
        self.cursor = min(len(self.text), self.cursor + 1)

    def home(self) -> None:
        self.cursor = self.text.rfind("\n", 0, self.cursor) + 1

    def end(self) -> None:
        nl = self.text.find("\n", self.cursor)
        self.cursor = len(self.text) if nl == -1 else nl

    def clear(self) -> None:
        self.text = ""
        self.cursor = 0

    @property
    def lines(self) -> list[str]:
        return self.text.split("\n")

    @property
    def cursor_line_col(self) -> tuple[int, int]:
        """The cursor's (logical line index, column) — ignores wrapping."""
        before = self.text[: self.cursor]
        return before.count("\n"), self.cursor - (before.rfind("\n") + 1)


def wrapped_rows(width: int, *line_widths: int) -> int:
    """How many terminal rows a set of lines occupies once the terminal wraps
    them at `width` columns (an empty line still takes one row)."""
    if width <= 0:
        return len(line_widths)
    return sum(max(1, math.ceil(w / width)) for w in line_widths)
