"""The autonomous rhythms — one place that answers "when does each self-directed
behavior's deterministic floor next lift?"

The cadences (settle, reflection, sleeping, the outreach budget, the research
queue) used to live inline in `reflect_idle` / `should_dream` /
`consider_outreach`. They live here now so the brain that *gates* on them and the
monitor that *displays* them read the same numbers and can never drift.

The snapshot is strictly read-only: it computes *earliest-eligible* times — when
a deterministic floor lifts — never a prediction. Once a floor is clear the brain
still decides whether and when to actually act.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from ..clock import Clock, now_utc
from ..config.config import Config
from ..store import Store
from .proactivity import ProactivityGovernor

# ── reflection cadence (the one home; brain.reflect_idle reads these) ──────────
# Reflection is *private*, so it doesn't need the outreach "conversation is over"
# bar (30 min) — only a short quiet so it isn't musing redundantly while reply
# turns are already forming thoughts. That settle is the *only* pacing a reflection
# on fresh material needs: a fresh reflection requires a new message since the last
# one, and the settle already makes that message ≥ this old, so there's no separate
# "fresh cooldown" (it would never bite). Musing on *unchanged* material has no new
# message to pace it, so it gets an explicit re-fire interval that backs off toward
# the cap when it keeps coming up empty.
REFLECT_SETTLE_MINUTES = 10  # quiet before reflecting is eligible (vs outreach's 30)
REFLECT_MUSE_BACKOFF_MAX_HOURS = 6  # ceiling as the muse interval backs off on repeated empty muses
# The base muse interval (wait after a muse before musing again on unchanged
# material) is user-tunable; it lives in config (`cadence.reflect_muse_interval_minutes`)
# and is threaded into `reflect_required_hours` by `reflection_gate`. This literal
# is only the fallback for direct/test callers.
_MUSE_INTERVAL_FALLBACK_MINUTES = 30

# app_state keys for idle reflection — written by reflect_idle, read by snapshot.
REFLECT_LAST_KEY = "reflect_last_at"
REFLECT_SEEN_KEY = "reflect_seen_at"
REFLECT_DRY_KEY = "reflect_dry"

# ── sleep/dream guards (the one home; dream.should_dream reads these) ──────────
SLEEP_SETTLE_MINUTES = 10  # don't dream within this of a message (it ends the session)
DREAM_INTERVAL_MINUTES = 90  # space between dreams within one sleep window (repeats nightly)


def reflect_required_hours(
    *, fresh: bool, dry: int, interval_minutes: float = _MUSE_INTERVAL_FALLBACK_MINUTES
) -> float:
    """How long since the last reflection before another may run. Fresh material
    is paced by the settle floor alone (0 here); musing on unchanged material
    backs off toward a cap when it keeps coming up empty. `interval_minutes` is
    the user-tunable base interval (fed from config by `reflection_gate`)."""
    if fresh:
        return 0.0
    interval = interval_minutes / 60
    return min(interval * (1 + dry), REFLECT_MUSE_BACKOFF_MAX_HOURS)


@dataclass
class Rhythm:
    name: str  # settle | reflection | sleeping | outreach | research
    state: str  # cooling | eligible | quiet_hours | done_tonight | scheduled | queued | idle | …
    next_at: str | None  # ISO UTC the floor next lifts (the client counts down to it)
    detail: str  # one-line, human


def _iso(when: dt.datetime | None) -> str | None:
    return when.astimezone(dt.UTC).isoformat() if when is not None else None


def _parse(s: str | None) -> dt.datetime | None:
    return dt.datetime.fromisoformat(s) if s else None


async def snapshot(store: Store, config: Config) -> list[Rhythm]:
    """Every rhythm's current state + earliest-eligible time, for the monitor."""
    gov = ProactivityGovernor(config)
    clock = Clock(config.timezone)
    now_local = clock.now()
    now = now_utc()

    last_msg = _parse(await store.last_message_at())
    last_out = _parse(await store.last_outreach_at())
    activity = max([t for t in (last_msg, last_out) if t], default=None)
    settle_at = activity + dt.timedelta(hours=gov.settle_hours()) if activity else None
    settled = settle_at is None or now >= settle_at

    gate = await reflection_gate(store, gov, now_local, now)
    dgate = await dream_gate(store, gov, clock, now_local, now)
    return [
        _settle(settle_at, settled),
        Rhythm("reflection", gate.state, _iso(gate.next_at), gate.detail),
        Rhythm("sleeping", dgate.state, _iso(dgate.next_at), dgate.detail),
        await _outreach(store, gov, now_local, now, settle_at, settled),
        await _research(store, gov, now_local),
    ]


def _settle(settle_at: dt.datetime | None, settled: bool) -> Rhythm:
    if settle_at is None:
        return Rhythm("settle", "eligible", None, "no conversation yet")
    if settled:
        return Rhythm("settle", "eligible", None, "chat has settled")
    return Rhythm("settle", "cooling", _iso(settle_at), "waiting for the chat to settle")


@dataclass
class ReflectionGate:
    """Whether idle reflection may run right now — the *one* computation the brain
    gates on (`reflect_idle`) and the monitor displays. `next_at` is when the
    deterministic floor next lifts (for the countdown); `fresh`/`dry`/`last_msg`
    are what the brain needs to act and update its bookkeeping."""

    eligible: bool
    state: str  # eligible | cooling | quiet_hours | idle
    detail: str
    next_at: dt.datetime | None = None
    fresh: bool = False
    dry: int = 0
    last_msg: str | None = None


async def reflection_gate(store, gov, now_local: dt.datetime, now: dt.datetime) -> ReflectionGate:
    """The deterministic floor for idle reflection: not in the dream window, the
    chat settled (reflection's own short settle), and enough time since the last
    reflection (fresh is paced by settle; musing has its own backing-off interval).
    Pure reads — the single source of truth for both gating and display."""
    if gov.in_quiet_hours(now_local):
        return ReflectionGate(False, "quiet_hours", "paused for the night (dreaming's turn)",
                              next_at=gov.quiet_ends_at(now_local))
    last_msg = await store.last_message_at()
    if not last_msg:
        return ReflectionGate(False, "idle", "nothing to reflect on yet")
    last_out = await store.last_outreach_at()
    activity = max(t for t in (_parse(last_msg), _parse(last_out)) if t)
    settle_at = activity + dt.timedelta(minutes=REFLECT_SETTLE_MINUTES)
    if now < settle_at:
        return ReflectionGate(False, "cooling", "waiting for the chat to settle",
                              next_at=settle_at, last_msg=last_msg)
    seen = await store.get_state(REFLECT_SEEN_KEY)
    fresh = seen is None or last_msg > seen
    dry = int(await store.get_state(REFLECT_DRY_KEY) or 0)
    last_reflect = _parse(await store.get_state(REFLECT_LAST_KEY))
    required = reflect_required_hours(
        fresh=fresh, dry=dry, interval_minutes=gov.config.cadence.reflect_muse_interval_minutes
    )
    eligible_at = last_reflect + dt.timedelta(hours=required) if last_reflect else None
    label = "fresh material" if fresh else (f"musing (×{dry} dry)" if dry else "musing")
    if eligible_at is None or now >= eligible_at:
        return ReflectionGate(True, "eligible", f"{label} — ready",
                              fresh=fresh, dry=dry, last_msg=last_msg)
    return ReflectionGate(False, "cooling", label,
                          next_at=eligible_at, fresh=fresh, dry=dry, last_msg=last_msg)


@dataclass
class DreamGate:
    """Whether a dream may run right now — the *one* computation the heartbeat gates on
    (`dream.should_dream`) and the monitor displays. `next_at` is when the deterministic
    floor next lifts (for the countdown)."""

    eligible: bool
    state: str  # scheduled | cooling | resting | eligible
    detail: str
    next_at: dt.datetime | None = None


async def dream_gate(store, gov, clock, now_local: dt.datetime, now: dt.datetime) -> DreamGate:
    """The deterministic floor for dreaming: inside the sleep window, the companion has
    settled (a dream ends the session), and DREAM_INTERVAL_MINUTES since the last dream
    tonight — so Moko dreams *repeatedly* through the night, not once. Pure reads — the
    single source of truth for both the heartbeat gate and the monitor display."""
    if not gov.in_quiet_hours(now_local):
        next_open = gov.next_quiet_start(now_local)
        return DreamGate(False, "scheduled", f"sleep begins {clock.format(next_open, '%H:%M')}",
                         next_at=next_open)
    last_msg = _parse(await store.last_message_at())
    if last_msg is not None:
        idle_until = last_msg + dt.timedelta(minutes=SLEEP_SETTLE_MINUTES)
        if now < idle_until:
            return DreamGate(False, "cooling", "waiting for you to settle before dreaming",
                             next_at=idle_until)
    # Within the window: space dreams by the interval since the last one tonight.
    opened_utc = clock.to_utc(gov.quiet_opened_at(now_local)).isoformat()
    last_dream = _parse(await store.last_dream_started_at(opened_utc))
    if last_dream is not None:
        next_dream = last_dream + dt.timedelta(minutes=DREAM_INTERVAL_MINUTES)
        if now < next_dream:
            return DreamGate(False, "resting",
                             f"dreaming again {clock.format(next_dream, '%H:%M')}",
                             next_at=next_dream)
    return DreamGate(True, "eligible", "ready to dream")


async def _outreach(store, gov, now_local, now, settle_at, settled) -> Rhythm:
    if gov.in_quiet_hours(now_local):
        return Rhythm("outreach", "quiet_hours", _iso(gov.quiet_ends_at(now_local)),
                      "quiet hours — nothing unprompted")
    budget = gov.daily_budget()
    if budget <= 0:
        return Rhythm("outreach", "disabled", None, "proactivity off")
    since = (now - dt.timedelta(hours=24)).isoformat()
    used = await store.count_budget_outreach(since)
    if used >= budget:
        oldest = _parse(await store.oldest_budget_outreach_since(since))
        frees = oldest + dt.timedelta(hours=24) if oldest else None
        return Rhythm("outreach", "ceiling", _iso(frees),
                      f"daily budget {used}/{budget} — frees up")
    if not settled:
        return Rhythm("outreach", "cooling", _iso(settle_at),
                      f"settling ({used}/{budget} used today)")
    return Rhythm("outreach", "eligible", None, f"eligible ({used}/{budget} used today)")


async def _research(store, gov, now_local) -> Rhythm:
    if gov.in_quiet_hours(now_local):
        return Rhythm("research", "quiet_hours", _iso(gov.quiet_ends_at(now_local)), "quiet hours")
    n = await store.count_research_gaps()
    if n == 0:
        return Rhythm("research", "idle", None, "nothing queued")
    return Rhythm("research", "queued", None,
                  f"{n} gap{'s' if n != 1 else ''} queued; runs on idle ticks")
