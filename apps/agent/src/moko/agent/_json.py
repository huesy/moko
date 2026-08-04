"""Tolerant extraction of a JSON value from an LLM reply (which may wrap it in prose)."""

from __future__ import annotations

import json
from typing import Any


def json_between(text: str, open_ch: str, close_ch: str) -> Any | None:
    """Parse the outermost `open_ch`…`close_ch` slice of `text` as JSON, tolerant of stray
    prose around it. Returns the parsed value, or None when nothing parses — callers apply
    their own typed fallback (`or {}` / `or []`) and isinstance guard."""
    start, end = text.find(open_ch), text.rfind(close_ch)
    if start == -1 or end <= start:
        return None
    try:
        return json.loads(text[start : end + 1])
    except (ValueError, TypeError):
        return None
