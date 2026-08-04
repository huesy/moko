"""Moko's own tiny terminal-input framework.

Just enough to give `moko chat` a native-scrollback REPL with a multi-line
composer that renders in place — no external TUI dependency, full control.
Three small, single-purpose pieces:

  • keys.py    — decode raw terminal bytes into `Key` events (legacy + the
                 kitty keyboard protocol, so Shift+Enter is distinguishable).
  • editor.py  — a pure multi-line edit buffer (text + cursor).
  • prompt.py  — the async input + status bar; `emit()` prints above it into
                 the terminal's native scrollback.

The first two are pure and unit-tested; prompt.py is the thin terminal-I/O shell
over them.
"""

from .editor import Editor
from .keys import Key, KeyDecoder
from .prompt import Prompt

__all__ = ["Editor", "Key", "KeyDecoder", "Prompt"]
