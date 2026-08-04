"""The rhythm snapshot + the governor window helpers it relies on."""

import datetime as dt

import pytest

from moko.agent import rhythms
from moko.agent.proactivity import ProactivityGovernor
from moko.config.config import Config
from moko.store import Store

CFG = Config(
    companion_name="Jordan", timezone="UTC", sleep_start="23:00",
    sleep_end="07:00", proactivity="moderate",
)


@pytest.fixture
async def store(tmp_path):
    s = await Store.open(tmp_path / "moko.db")
    try:
        yield s
    finally:
        await s.close()


def _utc(h, m=0):
    return dt.datetime(2026, 6, 25, h, m, tzinfo=dt.UTC)


@pytest.fixture(autouse=True)
def _frozen_noon(monkeypatch):
    """Pin "now" to a daytime instant so the snapshot tests don't depend on the
    wall clock (CFG quiet hours are 23:00–07:00 UTC, so a real run between those
    hours flipped every rhythm to `quiet_hours`). Freeze every binding the
    snapshot reads — the store's timestamp source (`utc_isoformat` → `now_utc`),
    the rhythms module's own `now_utc`, and `Clock.now` — to the SAME instant, so
    logged outreaches and the snapshot agree."""
    fixed = _utc(12)
    monkeypatch.setattr("moko.clock.now_utc", lambda: fixed)
    monkeypatch.setattr(rhythms, "now_utc", lambda: fixed)
    monkeypatch.setattr("moko.clock.Clock.now", lambda self: fixed.astimezone(self.tz))


# ---- governor window helpers (one source of truth) ----

def test_quiet_window_boundaries():
    gov = ProactivityGovernor(CFG)  # quiet 23:00 → 07:00
    midnight = _utc(0, 30)
    assert gov.in_quiet_hours(midnight)
    assert gov.quiet_ends_at(midnight) == _utc(7, 0)  # wakes at 07:00
    # opened at 23:00 the previous day
    assert gov.quiet_opened_at(midnight) == _utc(23, 0) - dt.timedelta(days=1)

    noon = _utc(12)
    assert not gov.in_quiet_hours(noon)
    assert gov.quiet_ends_at(noon) is None
    assert gov.next_quiet_start(noon) == _utc(23, 0)  # opens tonight


def test_reflect_required_hours_backs_off():
    fresh = rhythms.reflect_required_hours(fresh=True, dry=0)
    assert rhythms.reflect_required_hours(fresh=False, dry=0) >= fresh
    # musing backs off with dry count, capped
    assert (
        rhythms.reflect_required_hours(fresh=False, dry=99)
        == rhythms.REFLECT_MUSE_BACKOFF_MAX_HOURS
    )


# ---- the snapshot ----

async def test_snapshot_shape_empty(store):
    snap = {r.name: r for r in await rhythms.snapshot(store, CFG)}
    assert set(snap) == {"settle", "reflection", "sleeping", "outreach", "research"}
    assert snap["settle"].state == "eligible"  # no conversation yet
    assert snap["sleeping"].state == "scheduled"  # outside the sleep window
    assert snap["sleeping"].next_at is not None  # counts down to tonight's window
    assert snap["outreach"].state == "eligible"  # budget free
    assert snap["research"].state == "idle"


async def test_outreach_ceiling_when_budget_spent(store):
    # moderate budget = 3; log 3 budget-counted outreaches
    for _ in range(3):
        await store.log_outreach("cli", "curiosity")
    snap = {r.name: r for r in await rhythms.snapshot(store, CFG)}
    assert snap["outreach"].state == "ceiling"
    assert snap["outreach"].next_at is not None  # frees up ~24h after the oldest


async def test_research_queue_counts_gaps(store):
    await store.add_gap("What is the capital of X?", "fact", resolution_path="research")
    snap = {r.name: r for r in await rhythms.snapshot(store, CFG)}
    assert snap["research"].state == "queued"
    assert "1 gap" in snap["research"].detail
