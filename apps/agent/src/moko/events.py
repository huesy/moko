"""Moko's observation bus — a best-effort, in-process publish/subscribe seam.

Publishers (the gateway, the store, the brain) call :func:`publish` and move
on, knowing nothing about who is listening. Observers call :func:`subscribe`
to get their own bounded queue of events. The web monitor is the first
observer; others — a JSONL recorder, a metrics exporter, a "narrate my
thinking to Telegram" relay, an anomaly watcher — plug in the *same* way,
with no change to any publisher.

Safety is the whole point of doing it as a bus: observation is strictly
downstream of Moko's real work.

- :func:`publish` never raises and never blocks. A broken observer cannot
  break a turn.
- Each subscriber owns a bounded queue that **drops the oldest event on
  overflow**. A slow or stuck observer can neither stall the gateway nor
  starve the other observers — it just misses events (and can see how many
  it dropped).

This is deliberately in-process only — enough for one companion and a
handful of local observers. If a remote/cross-process observer ever appears,
the clean move is a single subscriber that bridges this bus to a broker
(NATS/Redis/…); the publisher contract above does not change.
"""

from __future__ import annotations

import asyncio
from collections import deque
from typing import Any

from .clock import utc_isoformat

_QUEUE_MAX = 512  # per-subscriber backlog before the oldest event is dropped
_RING_MAX = 256  # recent events retained to backfill a freshly-attached observer


class Subscription:
    """One observer's view of the stream. Hold it, iterate :meth:`events`,
    and :meth:`close` (or let ``async for`` finish) when done."""

    def __init__(self, bus: EventBus, *, queue_max: int = _QUEUE_MAX) -> None:
        self._bus = bus
        self.queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=queue_max)
        self.dropped = 0

    def _offer(self, event: dict[str, Any]) -> None:
        """Enqueue without ever blocking. On overflow, drop the oldest so the
        live tail keeps flowing rather than the observer wedging publishing."""
        try:
            self.queue.put_nowait(event)
        except asyncio.QueueFull:
            try:
                self.queue.get_nowait()
                self.dropped += 1
                self.queue.put_nowait(event)
            except Exception:  # noqa: BLE001 — overflow handling is best-effort
                pass

    async def events(self):
        """Async-iterate events until the subscription is closed."""
        try:
            while True:
                yield await self.queue.get()
        finally:
            self.close()

    def close(self) -> None:
        self._bus._unsubscribe(self)


class EventBus:
    def __init__(self) -> None:
        self._subs: set[Subscription] = set()
        self._recent: deque[dict[str, Any]] = deque(maxlen=_RING_MAX)

    def publish(self, event: dict[str, Any], *, backfill: bool = True) -> None:
        """Fan one event out to every subscriber. Never raises, never blocks.
        Stamps ``at`` (UTC ISO-8601) if the publisher didn't.

        ``backfill=False`` streams the event to *live* subscribers but keeps it OUT of the
        recent ring — for high-volume, low-narrative events (a per-row ``learned`` write) that
        would otherwise cycle the 256-slot ring during an autonomous burst and evict the
        dream/thought/message story a just-opened monitor backfills."""
        if "at" not in event:
            event = {"at": utc_isoformat(), **event}
        if backfill:
            self._recent.append(event)
        for sub in tuple(self._subs):  # tuple: a subscriber may close mid-fan-out
            sub._offer(event)

    def subscribe(self, *, backfill: bool = True, queue_max: int = _QUEUE_MAX) -> Subscription:
        """Attach a new observer. With ``backfill`` (default) it immediately
        receives the recent ring buffer, so a just-opened browser isn't blank.
        ``queue_max`` bounds that observer's backlog before it drops oldest."""
        sub = Subscription(self, queue_max=queue_max)
        self._subs.add(sub)
        if backfill:
            for event in tuple(self._recent):
                sub._offer(event)
        return sub

    def _unsubscribe(self, sub: Subscription) -> None:
        self._subs.discard(sub)

    @property
    def subscriber_count(self) -> int:
        return len(self._subs)

    def recent(self, limit: int | None = None) -> list[dict[str, Any]]:
        items = list(self._recent)
        return items[-limit:] if limit else items


# Module-level singleton — like the logger. Cheap when no one is listening:
# publish just appends to a small deque and iterates an empty set.
BUS = EventBus()


def publish(event: dict[str, Any], *, backfill: bool = True) -> None:
    """Publish to the process-wide bus. The common entry point for publishers.
    ``backfill=False`` keeps a high-volume event out of the recent ring (live-only)."""
    BUS.publish(event, backfill=backfill)


def subscribe(*, backfill: bool = True, queue_max: int = _QUEUE_MAX) -> Subscription:
    """Subscribe to the process-wide bus. The common entry point for observers."""
    return BUS.subscribe(backfill=backfill, queue_max=queue_max)
