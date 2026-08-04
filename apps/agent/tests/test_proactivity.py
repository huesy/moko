"""The proactivity governor — the safety *fuses*. These tests prove the floor
holds: no pings in the dream window, the daily ceiling is never exceeded,
curiosity waits for the chat to settle, and necessity is always allowed. Backoff
and salience are no longer here — they're the brain's judgment now."""

import datetime as dt

from moko.agent.proactivity import ProactivityGovernor
from moko.config.config import Config


def _cfg(proactivity="moderate", sleep="23:00", wake="07:00") -> Config:
    return Config(
        companion_name="Andrew",
        timezone="Asia/Tokyo",
        sleep_start=sleep,
        sleep_end=wake,
        proactivity=proactivity,
    )


def _at(hour: int, minute: int = 0) -> dt.datetime:
    # Local wall-time; the governor only reads .time().
    return dt.datetime(2026, 6, 24, hour, minute, tzinfo=dt.UTC)


# ── quiet hours (highest-stakes floor) ─────────────────────────────────
def test_quiet_hours_fails_safe_on_a_malformed_window():
    # A bad dream window must make Moko QUIETER (treat as quiet → suppress
    # outreach), never license 24/7 pinging. The fuses only ever dial down.
    gov = ProactivityGovernor(_cfg(sleep="not-a-time", wake="also-bad"))
    assert gov.in_quiet_hours(_at(12)) is True  # noon, yet suppressed (fail-safe)


def test_nothing_unprompted_during_the_dream_window():
    gov = ProactivityGovernor(_cfg(sleep="23:00", wake="07:00"))
    for hour in (23, 0, 3, 6):  # inside the wrap-around window
        assert gov.in_quiet_hours(_at(hour))
        # even necessity waits until morning
        assert not gov.decide(kind="commitment", now=_at(hour)).allow
        assert gov.decide(kind="commitment", now=_at(hour)).reason == "quiet_hours"


def test_outside_quiet_hours_is_active():
    gov = ProactivityGovernor(_cfg(sleep="23:00", wake="07:00"))
    for hour in (7, 12, 18, 22):
        assert not gov.in_quiet_hours(_at(hour))


def test_same_day_window_does_not_wrap():
    gov = ProactivityGovernor(_cfg(sleep="01:00", wake="06:00"))
    assert gov.in_quiet_hours(_at(3))
    assert not gov.in_quiet_hours(_at(23))


def test_wake_boundary_is_exclusive_start_inclusive():
    gov = ProactivityGovernor(_cfg(sleep="23:00", wake="07:00"))
    assert gov.in_quiet_hours(_at(23, 0))  # start inclusive
    assert not gov.in_quiet_hours(_at(7, 0))  # wake exclusive — awake again


# ── daily ceiling (curiosity only) ─────────────────────────────────────
def test_curiosity_ceiling_caps_at_the_level():
    gov = ProactivityGovernor(_cfg("moderate"))  # 3/day
    assert gov.decide(kind="curiosity", now=_at(12), budget_used=2).allow
    denied = gov.decide(kind="curiosity", now=_at(12), budget_used=3)
    assert not denied.allow and denied.reason == "ceiling_reached"


def test_budget_scales_with_level():
    assert ProactivityGovernor(_cfg("gentle")).daily_budget() == 1
    assert ProactivityGovernor(_cfg("moderate")).daily_budget() == 3
    assert ProactivityGovernor(_cfg("chatty")).daily_budget() == 6
    assert ProactivityGovernor(_cfg("weird-unknown")).daily_budget() == 3  # safe default


def test_necessity_is_exempt_from_ceiling():
    gov = ProactivityGovernor(_cfg("gentle"))  # tiny ceiling
    d = gov.decide(kind="commitment", now=_at(9), budget_used=99)
    assert d.allow and d.reason == "necessity"


# ── settle cooldown ────────────────────────────────────────────────────
def test_curiosity_waits_for_the_chat_to_settle():
    # A short "don't talk over a live chat" floor: curiosity stays ineligible
    # until the conversation has been quiet ~30 min (since the last message OR
    # outreach). Level-independent — the brain decides *when* past this floor.
    for level in ("gentle", "moderate", "chatty"):
        gov = ProactivityGovernor(_cfg(level))
        soon = gov.decide(kind="curiosity", now=_at(12), hours_since_last=0.25)
        assert not soon.allow and soon.reason == "too_soon"  # 15 min — still warm
        later = gov.decide(kind="curiosity", now=_at(12), hours_since_last=0.75)
        assert later.allow  # 45 min — chat has settled, eligible (the brain still picks when)
    # necessity ignores the settle floor — a due reminder still fires
    assert gov.decide(kind="commitment", now=_at(12), hours_since_last=0.0).allow


def test_settle_floor_follows_the_configured_cadence():
    # The settle floor is a user knob: raise it to 60 min and 45 min of quiet is
    # no longer enough, where the 30-min default would have allowed it.
    cfg = _cfg("moderate")
    cfg.cadence.outreach_settle_minutes = 60
    gov = ProactivityGovernor(cfg)
    assert not gov.decide(kind="curiosity", now=_at(12), hours_since_last=0.75).allow  # 45 min
    assert gov.decide(kind="curiosity", now=_at(12), hours_since_last=1.25).allow  # 75 min


def test_healthy_curiosity_passes_the_fuses():
    gov = ProactivityGovernor(_cfg("moderate"))
    d = gov.decide(kind="curiosity", now=_at(12), budget_used=0)
    assert d.allow and d.reason == "ok"
