"""`moko setup` — the warm, guided first meeting.

The whole flow lives in :mod:`moko.cli.wizard` (a full-screen, stepped wizard
sharing its in-frame widgets with `moko config`). This module is just the entry
point the CLI dispatches to: it guards for a real terminal, runs the wizard, and
reports the outcome.
"""

from __future__ import annotations

import sys

from .. import i18n
from ..config import config as cfg
from ..theme import console
from . import wizard


def run() -> int:
    """Guard the terminal, run the wizard, and report how it went."""
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        # The full-screen wizard needs a real, interactive terminal; without one,
        # prompt navigation can't work — fail clearly instead of half-working.
        console.print(
            f"[moko]{i18n.t('nav.need_tty')}[/moko]\n"
            "[muted]Or set values one at a time with "
            "[accent]moko config KEY VALUE[/accent].[/muted]"
        )
        return 1

    wiz = wizard.Wizard(cfg.load())
    outcome = wiz.run()

    if outcome == "completed":
        wiz.finish()  # bare-console tail: pairing, sign-ins, service offer, summary
        return 0
    if outcome == "declined":
        console.print(f"[moko]{i18n.t('terms.declined', wiz.lang)}[/moko]")
        return 1
    # cancelled — a returning companion is left exactly as they were; a first-timer
    # can come back any time.
    key = "welcome.kept" if wiz.had_prior else "welcome.cancelled"
    console.print(f"[moko]{i18n.t(key, wiz.lang)}[/moko]")
    return 0 if wiz.had_prior else 1
