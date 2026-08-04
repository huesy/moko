"""The observation bus: best-effort fan-out that can never break a turn."""

import asyncio

from moko.events import EventBus


async def test_subscriber_receives_published():
    bus = EventBus()
    sub = bus.subscribe()
    bus.publish({"type": "thought", "body": "hm"})
    event = await asyncio.wait_for(sub.queue.get(), timeout=1)
    assert event["type"] == "thought"
    assert event["body"] == "hm"
    assert "at" in event  # publish stamps a timestamp


async def test_publish_stamps_at_only_if_missing():
    bus = EventBus()
    sub = bus.subscribe()
    bus.publish({"type": "x", "at": "2026-01-01T00:00:00+00:00"})
    event = await asyncio.wait_for(sub.queue.get(), timeout=1)
    assert event["at"] == "2026-01-01T00:00:00+00:00"


async def test_backfill_delivers_recent_to_late_subscriber():
    bus = EventBus()
    bus.publish({"type": "a"})
    bus.publish({"type": "b"})
    sub = bus.subscribe(backfill=True)  # subscribes AFTER the events
    first = await asyncio.wait_for(sub.queue.get(), timeout=1)
    second = await asyncio.wait_for(sub.queue.get(), timeout=1)
    assert [first["type"], second["type"]] == ["a", "b"]


async def test_no_backfill_when_disabled():
    bus = EventBus()
    bus.publish({"type": "a"})
    sub = bus.subscribe(backfill=False)
    assert sub.queue.empty()


async def test_overflow_drops_oldest_not_newest():
    bus = EventBus()
    sub = bus.subscribe(backfill=False, queue_max=2)
    for i in range(5):
        bus.publish({"type": "n", "i": i})
    # Only the two most-recent survive; the oldest were dropped, never the live tail.
    drained = [sub.queue.get_nowait()["i"] for _ in range(sub.queue.qsize())]
    assert drained == [3, 4]
    assert sub.dropped == 3


async def test_publish_never_raises_without_subscribers():
    bus = EventBus()
    bus.publish({"type": "lonely"})  # must not raise


async def test_unsubscribe_stops_delivery():
    bus = EventBus()
    sub = bus.subscribe()
    assert bus.subscriber_count == 1
    sub.close()
    assert bus.subscriber_count == 0
    bus.publish({"type": "after"})  # delivered to no one; no error
    assert sub.queue.empty()


async def test_one_broken_observer_does_not_starve_another():
    bus = EventBus()
    full = bus.subscribe(backfill=False, queue_max=1)  # will overflow + drop
    healthy = bus.subscribe(backfill=False)
    for i in range(4):
        bus.publish({"type": "n", "i": i})
    # The healthy observer still got every event despite the other overflowing.
    got = [healthy.queue.get_nowait()["i"] for _ in range(healthy.queue.qsize())]
    assert got == [0, 1, 2, 3]
    assert full.dropped > 0
