"""Moko's sense of time — one source of "now".

Timestamps are stored in UTC and *rendered/reasoned about* in the
companion's configured timezone, never a hard-coded one (see MISSION.md
§ Temporal awareness). Nothing else in the codebase should call the wall
clock directly — go through here.
"""

from __future__ import annotations

import datetime as dt
from zoneinfo import ZoneInfo

UTC = dt.UTC


def now_utc() -> dt.datetime:
    """The current instant, timezone-aware, in UTC."""
    return dt.datetime.now(tz=UTC)


def utc_isoformat() -> str:
    """`now_utc()` as an ISO-8601 string — the canonical stored form."""
    return now_utc().isoformat()


class Clock:
    """Time as the companion experiences it.

    Holds a configured timezone and converts between it and UTC. A naive
    datetime handed to `to_local` is assumed UTC (our storage form);
    handed to `to_utc` it is assumed to be local wall-time.
    """

    def __init__(self, timezone: str) -> None:
        self.timezone = timezone
        self.tz = ZoneInfo(timezone)

    def now(self) -> dt.datetime:
        """Current local time in the configured timezone."""
        return dt.datetime.now(tz=self.tz)

    def to_local(self, when: dt.datetime) -> dt.datetime:
        """Render a stored (UTC) instant in the configured timezone."""
        if when.tzinfo is None:
            when = when.replace(tzinfo=UTC)
        return when.astimezone(self.tz)

    def to_utc(self, when: dt.datetime) -> dt.datetime:
        """Convert a local wall-time to the canonical UTC storage form."""
        if when.tzinfo is None:
            when = when.replace(tzinfo=self.tz)
        return when.astimezone(UTC)

    def format(self, when: dt.datetime | None = None, fmt: str = "%Y-%m-%d %H:%M %Z") -> str:
        """Human-readable local rendering of an instant (defaults to now)."""
        when = when if when is not None else self.now()
        return self.to_local(when).strftime(fmt)
