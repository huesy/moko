"""Best-effort detection of the system timezone, for the setup wizard.

Moko is always time-aware, so setup pre-selects the machine's timezone in the
(filterable) zone list — the companion just confirms or searches. Detection is
advisory only; the IANA name they actually pick is what's stored.
"""

from __future__ import annotations

import os
from pathlib import Path
from zoneinfo import available_timezones


def detect_timezone() -> str | None:
    """Best-effort guess at the system timezone (an IANA name), or None."""
    zones = available_timezones()

    env = os.environ.get("TZ")
    if env and env in zones:
        return env

    etc = Path("/etc/timezone")
    if etc.exists():
        name = etc.read_text().strip()
        if name in zones:
            return name

    link = Path("/etc/localtime")
    if link.is_symlink():
        target = os.readlink(link)
        if "zoneinfo/" in target:
            name = target.split("zoneinfo/", 1)[1]
            if name in zones:
                return name

    return None
