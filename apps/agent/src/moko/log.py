"""Structured logging to ~/.moko/logs/.

Two streams:
- ``moko.log`` — human-readable, rotating app log.
- ``llm.jsonl`` — one JSON line per LLM call (system prompt, messages,
  response, usage, and the parsed extraction). This is the window into
  what Moko actually sent and thought; invaluable while we're tuning.

No-op until ``configure()`` is called (the CLI does this), so library use
and tests never touch disk.
"""

from __future__ import annotations

import asyncio
import json
import logging
from logging.handlers import RotatingFileHandler

from .clock import utc_isoformat
from .config.paths import ensure_home, logs_dir

_enabled = False
_jsonl_handler: RotatingFileHandler | None = None


def _get_jsonl_handler() -> RotatingFileHandler:
    """A rotating handler dedicated to ``llm.jsonl``. It bounds the file the same
    way ``moko.log`` is bounded — an always-on daemon writes multi-KB records per
    call, so this can't be an unbounded ``open("a")``. The formatter emits the
    record verbatim, keeping the one-JSON-line-per-call format identical."""
    global _jsonl_handler
    if _jsonl_handler is None:
        ensure_home()
        handler = RotatingFileHandler(
            logs_dir() / "llm.jsonl", maxBytes=5_000_000, backupCount=3, encoding="utf-8"
        )
        handler.setFormatter(logging.Formatter("%(message)s"))  # raw JSON line, nothing added
        _jsonl_handler = handler
    return _jsonl_handler


def configure(level: int = logging.INFO) -> None:
    """Attach a rotating file handler to the ``moko`` logger. Idempotent."""
    global _enabled
    if _enabled:
        return
    ensure_home()
    handler = RotatingFileHandler(
        logs_dir() / "moko.log", maxBytes=2_000_000, backupCount=3, encoding="utf-8"
    )
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    logger = logging.getLogger("moko")
    logger.setLevel(level)
    logger.handlers.clear()
    logger.addHandler(handler)
    logger.propagate = False
    _enabled = True


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(f"moko.{name}")


def _append_line(line: str) -> None:
    handler = _get_jsonl_handler()
    # A bare LogRecord carrying the JSON line as its whole message; the "%(message)s"
    # formatter writes it as-is (plus the handler's newline) and rotates past the cap.
    record = logging.makeLogRecord({"msg": line})
    handler.emit(record)


def log_exchange(record: dict) -> None:
    """Append one LLM call to ``llm.jsonl``. No-op until configured.

    The file write runs OFF the event loop (fire-and-forget in a thread) so it
    never blocks the daemon's single serial consumer — the hard 'no blocking I/O
    on the loop' rule. A sync fallback covers tests/CLI where no loop is running."""
    if not _enabled:
        return
    line = json.dumps({"at": utc_isoformat(), **record}, ensure_ascii=False, default=str)
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        _append_line(line)  # no loop (tests, CLI) → write inline
        return
    loop.run_in_executor(None, _append_line, line)  # fire-and-forget; logging mustn't stall a turn
