"""The proactivity governor — the safety *fuses* for reaching out.

These are the deterministic floor, and only the floor. The judgment of *when* to
reach out, *whether* now is a good moment, and *what* to say is the brain's
(see `Brain.consider_outreach`); the governor exists so a buggy or
prompt-injected brain can never talk Moko into doing something unsafe. It can
only make Moko *quieter*, never louder. Three fuses, all unit-testable:

- **Quiet hours.** Nothing unprompted during the companion's sleep window
  (their configured timezone) — not even a reminder; it waits for morning.
- **Daily ceiling.** A hard cap on curiosity-type outreach per rolling 24h —
  an anti-runaway backstop, not a pacer (the brain paces). Necessity (a due
  commitment, a well-being check, a consent request) is exempt.
- **Settle cooldown.** Curiosity stays ineligible until the chat has been quiet
  a short while — a cheap "don't talk over a live conversation" floor.

Backoff-on-silence and salience are deliberately *not* here anymore: repeating
an unanswered question, or judging whether a low-value curiosity is worth a
ping, is exactly the human judgment the brain makes with full context. Pure
decision logic: it takes the local time and a couple of counts (the caller reads
those from `outreach`), calls no clock, and touches no store.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from ..config.config import Config

# Hard ceiling on curiosity outreaches per rolling 24h, by configured proactivity
# level — an anti-runaway fuse, not a pacer (the brain decides actual cadence
# well under this). Necessity is never counted against it. Unknown level →
# moderate.
OUTREACH_BUDGET_LEVELS = {"gentle": 1, "moderate": 3, "chatty": 6}
OUTREACH_BUDGET_DEFAULT = 3  # fallback when the proactivity level is unrecognized

# The rolling window the budget is counted over. Names the period, so "how many
# per window" stays honest even if the window changes.
OUTREACH_BUDGET_WINDOW_HOURS = 24

# The "don't talk over a live conversation" settle floor — how long the chat must
# be quiet before unprompted curiosity is eligible — is a user-tunable knob; it
# lives in config (`cadence.outreach_settle_minutes`), read in `settle_hours`.

# Kinds exempt from the ceiling — things the companion needs or asked for.
NECESSITY_KINDS = frozenset({"commitment", "urgent", "wellbeing", "consent_request"})


@dataclass(frozen=True)
class Decision:
    allow: bool
    reason: str  # machine label: ok | necessity | quiet_hours | too_soon | ceiling_reached


def parse_hhmm(value: str) -> dt.time:
    hh, _, mm = value.partition(":")
    return dt.time(int(hh), int(mm or 0))


def _within_window(now: dt.time, start: dt.time, end: dt.time) -> bool:
    """Is `now` inside [start, end)? Handles a window that wraps midnight
    (e.g. 23:00 → 07:00). An empty window (start == end) is never active."""
    if start == end:
        return False
    if start < end:
        return start <= now < end
    return now >= start or now < end  # wraps past midnight


def window_opened_at(now: dt.datetime, sleep_start: str) -> dt.datetime:
    """The most recent sleep/quiet-hours opening at or before `now` (same tz as
    `now`). The one place this is computed — shared by `should_dream` and the
    rhythms snapshot so they can't drift."""
    start = parse_hhmm(sleep_start)
    opened = now.replace(hour=start.hour, minute=start.minute, second=0, microsecond=0)
    if opened > now:
        opened -= dt.timedelta(days=1)
    return opened


class ProactivityGovernor:
    def __init__(self, config: Config) -> None:
        self.config = config

    def daily_budget(self) -> int:
        return OUTREACH_BUDGET_LEVELS.get(
            (self.config.proactivity or "").lower(), OUTREACH_BUDGET_DEFAULT
        )

    def settle_hours(self) -> float:
        return self.config.cadence.outreach_settle_minutes / 60

    def in_quiet_hours(self, now: dt.datetime) -> bool:
        """`now` is local wall-time in the companion's configured timezone."""
        try:
            start = parse_hhmm(self.config.sleep_start)
            end = parse_hhmm(self.config.sleep_end)
        except (ValueError, AttributeError):
            # Fail SAFE, not open: a malformed window must make Moko quieter (treat
            # as quiet → suppress unprompted outreach), never license 24/7 pinging.
            # The fuses can only ever dial Moko down. (Config validation is the real
            # fix; this guarantees the floor holds even if a bad value slips through.)
            return True
        return _within_window(now.time(), start, end)

    def quiet_opened_at(self, now: dt.datetime) -> dt.datetime:
        """The most recent quiet-hours opening at/before `now` (local tz)."""
        return window_opened_at(now, self.config.sleep_start)

    def next_quiet_start(self, now: dt.datetime) -> dt.datetime:
        """The next quiet-hours opening strictly after `now` (local tz)."""
        opened = self.quiet_opened_at(now)
        return opened + dt.timedelta(days=1) if opened <= now else opened

    def quiet_ends_at(self, now: dt.datetime) -> dt.datetime | None:
        """If `now` is inside quiet hours, when they next end (local tz); else None."""
        if not self.in_quiet_hours(now):
            return None
        end = parse_hhmm(self.config.sleep_end)
        ends = now.replace(hour=end.hour, minute=end.minute, second=0, microsecond=0)
        if ends <= now:
            ends += dt.timedelta(days=1)
        return ends

    def decide(
        self,
        *,
        kind: str,
        now: dt.datetime,
        budget_used: int = 0,
        hours_since_last: float | None = None,
    ) -> Decision:
        """Whether an outreach of `kind` clears the safety fuses right now — the
        floor, not the judgment. `budget_used` and `hours_since_last` come from
        `outreach` (the caller reads them). A pass means "allowed"; the brain
        then decides whether to actually reach out."""
        if self.in_quiet_hours(now):
            return Decision(False, "quiet_hours")  # nothing unprompted while they sleep
        if kind in NECESSITY_KINDS:
            return Decision(True, "necessity")  # exempt from ceiling + settle
        # Curiosity path from here.
        if hours_since_last is not None and hours_since_last < self.settle_hours():
            # Hours since the last activity (message or outreach). Don't talk over
            # a live chat — wait for the lull.
            return Decision(False, "too_soon")
        if budget_used >= self.daily_budget():
            return Decision(False, "ceiling_reached")
        return Decision(True, "ok")
