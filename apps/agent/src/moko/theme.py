"""Moko's look — pink, to match Moko themself.

One place for the accent colour so the setup wizard (questionary) and all
terminal output (rich) stay visually consistent.
"""

from __future__ import annotations

from questionary import Style
from rich.console import Console
from rich.theme import Theme

# Warm rose-pink, taken from the mascot.
MOKO_PINK = "#ff6fae"
MOKO_PINK_DIM = "#d6568c"

# Styling for questionary prompts (arrow-key select, SPACE-toggle, etc.).
QUESTIONARY_STYLE = Style(
    [
        ("qmark", f"fg:{MOKO_PINK} bold"),  # the leading "?"
        ("question", "bold"),
        ("answer", f"fg:{MOKO_PINK} bold"),  # the chosen answer
        ("pointer", f"fg:{MOKO_PINK} bold"),  # the ▸ cursor
        ("highlighted", f"fg:{MOKO_PINK} bold"),  # hovered option
        ("selected", f"fg:{MOKO_PINK}"),  # ticked checkbox
        ("separator", f"fg:{MOKO_PINK_DIM}"),
        ("instruction", "fg:#8a8a8a italic"),
        ("text", ""),
        ("disabled", "fg:#6a6a6a italic"),
    ]
)

# rich theme + a shared console.
RICH_THEME = Theme(
    {
        "moko": MOKO_PINK,
        "moko.dim": MOKO_PINK_DIM,
        "accent": f"bold {MOKO_PINK}",
        "muted": "#8a8a8a",
    }
)

console = Console(theme=RICH_THEME)
