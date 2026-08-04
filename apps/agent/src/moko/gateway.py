"""The gateway — Moko's always-on heart.

One event queue, one consumer: every inbound message and every heartbeat
tick is processed serially, so there's a single writer to the graph and no
races in subject resolution or session rotation. A turn is one LLM
interaction: Moko replies and records (via the `remember` tool) in the same
loop, then the gateway sends — no separate extraction call to wait on.

Deliberately a single queue + consumer, not a pub/sub bus — enough for one
companion and a channel or two. We add more structure only when a real
second caller appears.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Iterable
from dataclasses import dataclass

from .agent import Brain
from .agent.brain import OutreachProposal
from .agent.dream import dream, should_dream
from .channels import Channel, Inbound
from .clock import now_utc
from .config.paths import COMMAND_TIMEOUT_S, control_socket_path, db_path
from .events import publish as _publish_event
from .log import get_logger
from .store import Store, backup

_log = get_logger("gateway")

# Approving a sensitive action is a SAFETY decision, matched deterministically
# on the companion's own (authenticated) reply — never judged by the prompt-
# injectable LLM. We require a CLEAN, whole-message yes/no: anything longer or
# ambiguous ("okay but actually don't", "notify Sarah") falls through to
# "unclear" and is handled as an ordinary message, never as approval. Fail
# closed — only an explicit affirmation runs the action.
_APPROVE = {
    "yes", "yep", "yeah", "ya", "yup", "ok", "okay", "k", "sure", "approve",
    "approved", "go", "go ahead", "do it", "please do", "yes please", "confirm",
    "confirmed", "y",
}
_ALWAYS = {"yes always", "always", "yes, always", "approve always", "always allow"}
_DENY = {
    "no", "nope", "nah", "cancel", "stop", "don't", "dont", "deny", "denied",
    "n", "no thanks", "never",
}
# The heartbeat tick: how often Moko wakes to consider acting (dream / outreach /
# research / reflect). Every cadence floor is a gate this tick checks.
HEARTBEAT_SECONDS = 30

APPROVAL_TTL_MINUTES = 15  # a pending approval expires if ignored this long
TYPING_REFRESH_SECONDS = 4  # Telegram clears "typing…" after ~5s, so refresh under that

# `moko outreach/reflect/research/dream` hand their action to the *running daemon*
# over a local control socket (see config.paths.control_socket_path), so the daemon
# executes it (single writer, publishes to the bus → shows in the feed) rather than
# a second process. Unix socket under ~/.moko — localhost-only, owner-only (0600);
# no network, no token (commanding the daemon is the same trust as running the CLI).
# COMMAND_TIMEOUT_S (the run budget) is paired with the CLI's read timeout in
# config.paths, so the two magic numbers can't drift.


@contextlib.asynccontextmanager
async def _typing(channel, should_type=None):
    """Keep a channel's "typing…" indicator alive for the duration of a turn.
    No-op for channels without one (the default `typing()` does nothing).

    `should_type`, if given, gates each refresh — so while the turn is blocked
    waiting on the companion's approval we stop signalling "typing…" (it's *them*
    we're waiting on, not the other way round)."""

    async def _loop():
        while True:
            # Best-effort — a typing blip must never sink a turn. CancelledError
            # is a BaseException, so cancellation still breaks the loop cleanly.
            if should_type is None or should_type():
                with contextlib.suppress(Exception):
                    await channel.typing()
            await asyncio.sleep(TYPING_REFRESH_SECONDS)

    task = asyncio.create_task(_loop()) if channel is not None else None
    try:
        yield
    finally:
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task


def _approval_verdict(text: str) -> str:
    """approve | always | deny | unclear — from a clean, whole-message reply."""
    t = text.strip().lower().rstrip("!.? ")
    if t in _ALWAYS:
        return "always"
    if t in _APPROVE:
        return "approve"
    if t in _DENY:
        return "deny"
    return "unclear"


@dataclass
class Tick:
    """The heartbeat: a periodic nudge to *consider* acting (M5/M6 decide)."""


@dataclass
class Command:
    """An out-of-band instruction handed to the living daemon over the control
    socket (`moko outreach/reflect/research/dream`). It rides the one consumer
    queue like any event, so it writes through the single writer and publishes to
    the bus normally — it shows in the feed because it genuinely *is* the daemon
    acting, just on command rather than on its own clock. `result` is resolved with
    an (ok, text) pair — the human-readable outcome plus whether it succeeded, so the
    socket can prefix a status byte and the CLI can exit non-zero on a failure."""

    action: str
    result: asyncio.Future


class Gateway:
    def __init__(
        self,
        store: Store,
        brain: Brain,
        channels: Iterable[Channel],
        *,
        heartbeat_seconds: float = HEARTBEAT_SECONDS,
    ) -> None:
        self.store = store
        self.brain = brain
        self.channels = list(channels)
        self._by_name = {c.name: c for c in self.channels}
        self.heartbeat_seconds = heartbeat_seconds
        self.queue: asyncio.Queue = asyncio.Queue()
        # When a turn is blocked awaiting approval, this holds (approval_id,
        # future). The companion's reply resolves the future from the *channel's*
        # receive task (see `_emit`), so the blocked turn continues inline without
        # the single consumer deadlocking on a message it can't dequeue.
        self._approval: tuple[str, asyncio.Future] | None = None
        # Set when the companion replies to a pending approval with something other
        # than a clean yes/no — they're redirecting. The in-flight turn is abandoned
        # (its reply suppressed) and their message runs as a fresh turn instead.
        self._redirected = False
        # The turn already told the companion its outcome directly (a redirect set-
        # aside, or a timeout closure), so the gateway suppresses the turn's own
        # wrap-up reply — they get one clear message, not two.
        self._reply_suppressed = False
        # True only while a turn is parked on `_await_approval` — pauses "typing…".
        self._awaiting_approval = False
        # Coalesce heartbeats: at most one unhandled Tick in the queue at a time, so
        # a long block (a 15-min approval, a multi-minute dream) doesn't drain a
        # thundering batch of stale ticks afterward. Set when a Tick is enqueued,
        # cleared when one is handled.
        self._tick_pending = False
        # How many inbound messages are queued but not yet picked up by the consumer.
        # Incremented as an inbound is enqueued (`_emit`), decremented as the consumer
        # starts handling it (`handle`). A running dream polls this via `should_yield`
        # so it can stop early when the companion is waiting — an explicit counter, NOT
        # `queue.empty()`, since the coalesced Tick keeps the queue non-empty.
        self._inbound_waiting = 0

    async def _emit(self, inbound: Inbound) -> None:
        # If a turn is suspended awaiting approval, the companion's answer resolves
        # it here — in the channel's own task, not the blocked consumer. A clean
        # yes/no decides it and is consumed (not enqueued). Anything else is a
        # *redirect*: they typed a new instruction instead of answering, so we drop
        # the pending action, mark the turn redirected (its reply is suppressed),
        # and let this message run as a fresh turn whose reply takes its place.
        appr = self._approval
        if appr is not None and not appr[1].done():
            verdict = _approval_verdict(inbound.text)
            if verdict in ("approve", "always", "deny"):
                appr[1].set_result(verdict)
                return
            self._redirected = True
            self._reply_suppressed = True  # the new message's reply replaces this turn's
            appr[1].set_result("redirect")
        # An inbound is now waiting for the consumer — count it so a running dream can
        # see the companion needs it and yield. A clean-yes/no approval resolves above
        # and returns without enqueuing, so it isn't (and shouldn't be) counted; a
        # redirect falls through here and *is* counted (it runs as a fresh turn).
        self._inbound_waiting += 1
        await self.queue.put(inbound)

    async def handle(self, event) -> None:
        if isinstance(event, Tick):
            self._tick_pending = False  # this tick is being handled — allow the next
            await self._on_tick()
            return
        if isinstance(event, Command):
            await self._run_command(event)
            return

        # An inbound is now being handled, so it's no longer "waiting" — decrement the
        # counter a running dream polls (floored at 0, so a stray decrement can't drive
        # it negative and mask a genuinely waiting message).
        self._inbound_waiting = max(0, self._inbound_waiting - 1)

        # Any inbound means the companion is engaged — reset the outreach backoff
        # and drop any stale "wait until X" defer (decided while they were quiet, so
        # a fresh message makes it obsolete).
        await self.store.mark_outreach_engaged()
        await self.brain.clear_reconsider()
        self._redirected = False  # fresh turn — no redirect pending
        self._reply_suppressed = False
        self._awaiting_approval = False

        # Channels decide who's allowed to speak (Telegram filters by id; the
        # local terminal is trusted). Show "typing…" while the (potentially slow)
        # turn runs. A sensitive action inside the turn blocks inline on
        # `_await_approval`; the turn just continues once decided.
        channel = self._by_name.get(event.channel)
        _publish_event({"type": "turn.start", "channel": event.channel, "text": event.text})

        def _on_tool(e: dict) -> None:
            _publish_event({"type": "tool", **e})

        async def _request_approval(
            payload: str, description: str, category: str, always_ok: bool
        ) -> str:
            return await self._await_approval(channel, payload, description, category, always_ok)

        async def _send_file(*, data, filename, caption, kind) -> None:
            await channel.send_file(data, filename=filename, caption=caption, kind=kind)

        # Only offer the send path on a channel that can actually upload files.
        send_file = _send_file if getattr(channel, "can_send_files", False) else None

        try:
            async with _typing(channel, should_type=lambda: not self._awaiting_approval):
                turn = await self.brain.respond(
                    event.text, channel=event.channel,
                    on_event=_on_tool, request_approval=_request_approval,
                    attachments=getattr(event, "attachments", None),
                    send_file=send_file,
                )
        except Exception:
            # A failed turn (provider down, exhausted retry, a bug) must not leave
            # the companion in silence — that reads as Moko ignoring them. Degrade
            # to a short, honest apology instead. (CancelledError is a BaseException,
            # so shutdown still propagates.)
            _log.warning("turn failed; sending an honest fallback", exc_info=True)
            _publish_event({"type": "turn.error", "channel": event.channel})
            if channel is not None:
                with contextlib.suppress(Exception):
                    await channel.send(
                        "Sorry — I hit a snag reaching my thoughts just now and couldn't "
                        "finish that. Could you try me again in a moment?"
                    )
            return
        _publish_event(
            {
                "type": "turn.end",
                "channel": event.channel,
                "recorded": turn.recorded,
                "input_tokens": turn.input_tokens,
                "output_tokens": turn.output_tokens,
            }
        )
        # Send unless the turn already spoke for itself (a redirect set-aside or a
        # timeout closure was delivered directly) or there's nothing to say (guarded:
        # an empty send would error and read as dead air).
        if channel is not None and not self._reply_suppressed and turn.reply.strip():
            await channel.send(turn.reply)

    async def _await_approval(
        self, channel, payload: str, description: str, category: str, always_ok: bool
    ) -> str:
        """Block until the companion decides on a sensitive action. Returns
        "approve" | "always" | "deny" | "redirect" | "timeout". The decision is
        delivered by `_emit` (the channel's task) resolving our future, so this
        await doesn't wedge the consumer. A TTL backstops silence (≠ consent).

        `always_ok` says whether this action's boundary class can be trusted with
        "always" (GATE-tier only) — the channel shows the button accordingly. If the
        companion has already redirected this turn, every further action the
        abandoned turn proposes is held without prompting again."""
        if self._redirected:
            return "redirect"
        aid = await self.store.add_approval(payload, category)
        msg_id = None
        if channel is not None:
            msg_id = await channel.request_approval(description, always_ok=always_ok)
        _publish_event({"type": "approval.request", "action": description})
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._approval = (aid, fut)
        self._awaiting_approval = True  # pause "typing…" — we're waiting on *them*
        try:
            decision = await asyncio.wait_for(fut, timeout=APPROVAL_TTL_MINUTES * 60)
        except TimeoutError:
            decision = "timeout"
        finally:
            self._approval = None
            self._awaiting_approval = False
            # Drop the buttons however it resolved, so a resolved prompt can never
            # be tapped later (a typed yes/no, a redirect, or a timeout all leave the
            # inline keyboard live otherwise). Best-effort.
            if channel is not None and msg_id is not None:
                await channel.clear_approval(msg_id)
        status = {
            "approve": "executed", "always": "executed",
            "deny": "denied", "redirect": "superseded", "timeout": "expired",
        }[decision]
        await self.store.set_approval_status(aid, status)
        _publish_event({"type": "approval.resolved", "verdict": decision})
        if decision == "timeout":
            # Don't leave them guessing: say plainly what lapsed, and suppress the
            # turn's own wrap-up so they get this one clear line, not two.
            self._reply_suppressed = True
            if channel is not None:
                with contextlib.suppress(Exception):
                    await channel.send(
                        f"I didn't hear back about {description}, so I've set that aside "
                        "for now — just say the word if you still want it."
                    )
        return decision

    async def _on_tick(self) -> None:
        """The heartbeat's decision. In the dream window Moko consolidates (and
        nothing else — the window is quiet by definition). Otherwise it may reach
        out: deterministic fuses gate it, then the Brain consults itself on
        whether/when/what (already phrased). If it decides to act, we send and
        record it. Each phase is isolated so a transient LLM or send failure is
        logged and retried next tick, never wedging the loop."""
        now = self.brain.clock.now()
        _publish_event({"type": "heartbeat"})
        if await should_dream(self.brain, now):
            try:
                await self._run_dream(now)
            except Exception:
                _log.warning("dream failed; will retry next window", exc_info=True)
            return

        # Deterministic reminders first: each commitment past due fires at its time with
        # its own message and completes only itself — never routed through the discretionary
        # consult below. (Batching all-due + feeding the chat into the consult once fired a
        # stale batch 10s after an unrelated ask, phrased "your hour's up".)
        try:
            for text, proposal in await self.brain.due_commitment_outreaches(now):
                try:
                    await self._deliver_outreach(text, proposal)
                    _log.info("reminder delivered: %s", proposal.topic[:60])
                except Exception:
                    _log.warning("reminder send failed; will retry next tick", exc_info=True)
        except Exception:
            _log.warning("due-commitment check failed; skipping this tick", exc_info=True)

        try:
            decision = await self.brain.consider_outreach(now)
        except Exception:
            _log.warning("outreach consult failed; skipping this tick", exc_info=True)
            decision = None
        if decision is None:
            # Idle: pursue one of Moko's own research-path curiosities from the web
            # (answers itself before ever asking the companion) — but ONLY if the
            # companion opted into unprompted autonomous research. It ingests untrusted
            # web content with no human watching (a standing injection surface), so it's
            # off by default; the gaps just wait in the queue until enabled. Read live,
            # so `moko config curiosity.autonomous_research on` takes hold without a
            # restart. Reflection (below) still runs — it never touches the network.
            if self.brain.config.allow_autonomous_research:
                try:
                    gap, _ = await self._run_research()
                except Exception:
                    _log.warning("research failed; will move on", exc_info=True)
                    return
                if gap is not None:
                    return  # researched this tick — done
            # Nothing to research → a quiet moment to *reflect*: form private
            # thoughts on its own. Gated (settle + interval + backoff), so most
            # idle ticks this is a cheap no-op with no LLM call. The brain
            # publishes its own `reflection` event (and the thoughts publish
            # themselves), so nothing to emit here.
            try:
                await self.brain.reflect_idle(now)
            except Exception:
                _log.warning("reflection failed; will move on", exc_info=True)
            return
        text, proposal = decision
        if not text:
            return
        try:
            await self._deliver_outreach(text, proposal)
        except Exception:
            # Send failed → record nothing, so the gap/commitment isn't falsely
            # marked done and `reconsider` isn't set; the next tick can try again.
            _log.warning("outreach send failed; not recording it", exc_info=True)
            return
        _log.info("proactive outreach sent (%s)", proposal.kind)

    async def _deliver_outreach(self, text: str, proposal: OutreachProposal) -> None:
        """Send a chosen outreach to every channel, then record it (budget, pacing,
        reconsider) and publish it. Shared by the heartbeat and the `outreach`
        command — recording only after a successful send (a failure raises, so the
        caller leaves it unrecorded to retry). A "show" outreach carries a photo
        (`proposal.photo_path`) — sent as a captioned image where the channel supports
        files, with a graceful fallback to the plain message where it doesn't."""
        photo = self._outreach_photo(proposal)
        for channel in self.channels:
            if photo is not None and getattr(channel, "can_send_files", False):
                data, name = photo
                await channel.send_file(data, filename=name, caption=text, kind="image")
            else:
                await channel.send(text)
        await self.brain.record_outreach(
            proposal, self.channels[0].name if self.channels else "none", text=text
        )
        _publish_event({"type": "outreach", "kind": proposal.kind, "text": text})

    async def _run_dream(self, now):
        """Publish a dream's start/end around the consolidation and return its stats. The end
        event fires even if the dream raises (the exception then propagates, so the caller —
        heartbeat or command — decides whether to swallow-and-retry or surface it). Shared so
        an autonomous beat and a commanded `moko dream` run the identical path.

        We hand the dream a `should_yield` it can poll between passes: True once an inbound is
        waiting, so a long consolidation stops early to answer the companion rather than making
        them wait it out."""
        _publish_event({"type": "dream.start"})
        stats = None
        try:
            stats = await dream(  # counts of what was consolidated
                self.brain, now, should_yield=lambda: self._inbound_waiting > 0
            )
        finally:
            _publish_event({"type": "dream.end", "stats": stats})
        return stats

    async def _run_research(self) -> tuple:
        """Pull the next research-path gap (if any), publish it, and answer it from the web.
        Returns (gap, recorded); gap is None when the queue is empty. Raises on failure — the
        caller decides whether to swallow it. Shared by the heartbeat and `moko research`."""
        gap = await self.store.next_research_gap()
        if gap is None:
            return None, False
        _publish_event({"type": "research", "gap": gap.description})
        recorded = await self.brain.research_gap(gap)
        return gap, recorded

    def _outreach_photo(self, proposal: OutreachProposal) -> tuple[bytes, str] | None:
        """Read the photo a "show" outreach wants to send, or None (no photo, or it
        couldn't be read → the outreach degrades to text rather than failing)."""
        if not proposal.photo_path:
            return None
        from pathlib import Path

        from .config.paths import media_dir
        try:
            path = media_dir() / proposal.photo_path
            return path.read_bytes(), Path(proposal.photo_path).name
        except OSError:
            _log.warning("show outreach: couldn't read its photo; sending text only")
            return None

    # ── commands (the `moko …` CLI, run by the daemon over the control socket) ──
    async def _run_command(self, cmd: Command) -> None:
        """Run a commanded action and resolve `cmd.result` with an (ok, text) pair.
        A crash resolves ok=False so the CLI can exit non-zero — otherwise a scripted
        `moko dream && …` runs its next step on a failed command. `_dispatch_command`
        itself still returns a plain string; we wrap success/failure here."""
        try:
            text = await self._dispatch_command(cmd.action)
            result = (True, text)
        except Exception as exc:  # noqa: BLE001 — surface any failure to the CLI
            _log.warning("command %r failed", cmd.action, exc_info=True)
            result = (False, f"That didn't work: {exc}")
        if not cmd.result.done():
            cmd.result.set_result(result)

    async def _dispatch_command(self, action: str) -> str:
        """Run one commanded action, exactly as the heartbeat would, and return a
        line for the CLI. Everything here writes via the store and publishes to the
        bus, so it lands in the monitor like any autonomous beat."""
        now = self.brain.clock.now()
        if action == "dream":
            await self._run_dream(now)
            latest = await self.store.recent_dreams(limit=1)
            return latest[0]["summary"] if latest else "Dreamt — not much to consolidate."
        if action == "reflect":
            recorded = await self.brain.reflect_idle(now, force=True)
            if not recorded:
                return "A quiet mind — nothing landed this time."
            latest = await self.store.recent_thoughts(limit=1)
            return latest[0].body if latest else "Formed a thought."
        if action == "research":
            if not self.brain.config.allow_autonomous_research:
                return (
                    "Autonomous research is off. Turn it on with "
                    "`moko config curiosity.autonomous_research on`."
                )
            gap, recorded = await self._run_research()
            if gap is None:
                return "No research questions queued."
            return (
                f"Looked into: {gap.description}" if recorded
                else "Couldn't find anything solid; set it aside."
            )
        if action in ("outreach", "outreach_force"):
            return await self._command_outreach(force=action == "outreach_force")
        if action == "snapshot" or action.startswith("snapshot "):
            return await self._command_snapshot(action)
        return f"I don't know the command {action!r}."

    async def _command_snapshot(self, action: str) -> str:
        """Back up the whole mind on demand. The daemon is the single writer, so a
        VACUUM INTO taken here (between events on the serial consumer) is clean and
        consistent. An optional label rides on the action line (`snapshot <label>`);
        without one it's stamped `manual-<time>`."""
        raw = action[len("snapshot"):].strip()
        label = backup.sanitize_label(raw) if raw else f"{backup.MANUAL_PREFIX}{backup.fs_stamp()}"
        target = await backup.create_snapshot(self.store.conn, db_path(), label)
        kb = target.stat().st_size // 1024
        _publish_event({"type": "snapshot", "name": target.name, "kb": kb})
        return f"Saved a snapshot of the mind: {target.name} ({kb} KB)"

    async def _command_outreach(self, *, force: bool) -> str:
        now = self.brain.clock.now()
        if force:  # just send the best candidate, no deliberation
            proposal = await self._pick_outreach()
            if proposal is None:
                return "Nothing to reach out about yet."
            text = await self.brain.phrase_outreach(proposal)
        else:  # the real decision — may act, may hold
            decision = await self.brain.consider_outreach(now, force=True)
            if decision is None:
                consults = await self.store.recent_outreach_decisions(limit=1)
                if consults and consults[0]["reasoning"]:
                    return f"Held off — {consults[0]['reasoning']}"
                return "Nothing to reach out about yet."
            text, proposal = decision
        await self._deliver_outreach(text, proposal)
        return f"Reached out: {text}"

    async def _pick_outreach(self) -> OutreachProposal | None:
        """The best candidate to raise, ignoring the timing governor (the --force
        path): a due reminder if any, else the top open curiosity."""
        due = await self.store.due_commitments(now_utc().isoformat())
        if due:
            return OutreachProposal(
                "commitment", "; ".join(c.description for c in due),
                counts_to_budget=False, commitment_ids=[c.id for c in due],
            )
        gap = await self.store.next_curiosity_gap()
        if gap is not None:
            return OutreachProposal("curiosity", gap.description,
                                    counts_to_budget=True, gap_id=gap.id)
        return None

    async def _control_server(self) -> None:
        """Listen on the local control socket so `moko …` commands run *here*, in
        the daemon. Localhost-only, owner-only; if the platform has no unix sockets
        we simply don't offer it (the CLI falls back to running in-process)."""
        path = control_socket_path()
        with contextlib.suppress(FileNotFoundError):
            path.unlink()  # clear a stale socket from a previous run
        try:
            server = await asyncio.start_unix_server(self._handle_control, path=str(path))
        except (NotImplementedError, OSError):
            _log.info("control socket unavailable; CLI commands will run in-process")
            return
        with contextlib.suppress(OSError):
            path.chmod(0o600)  # only the owner may command the daemon
        async with server:
            await server.serve_forever()

    async def _handle_control(self, reader, writer) -> None:
        """One control connection: read the action word, run it on the consumer,
        hand back a status byte + the result line. The reply is prefixed with '+'
        (ok) or '-' (the action failed) so a scripted `moko dream && …` sees the real
        exit code instead of always succeeding. A daemon-side timeout writes '+':
        the daemon still owns the action (it's working), so the CLI must not re-run
        it — that's the BUSY-as-exit-0 contract."""
        try:
            raw = await asyncio.wait_for(reader.readline(), timeout=5)
            action = raw.decode("utf-8", "replace").strip()
            if not action:
                return
            fut: asyncio.Future = asyncio.get_running_loop().create_future()
            await self.queue.put(Command(action, fut))
            try:
                ok, text = await asyncio.wait_for(fut, timeout=COMMAND_TIMEOUT_S)
            except TimeoutError:
                ok, text = True, "That took too long; the daemon is still working on it."
            prefix = "+" if ok else "-"
            writer.write((prefix + text + "\n").encode("utf-8"))
            await writer.drain()
        except Exception:
            _log.debug("control connection failed", exc_info=True)
        finally:
            with contextlib.suppress(Exception):
                writer.close()
                await writer.wait_closed()

    async def _consume(self) -> None:
        while True:
            event = await self.queue.get()
            try:
                await self.handle(event)
            except Exception:
                _log.warning("event handling failed; continuing", exc_info=True)
            finally:
                self.queue.task_done()

    async def _heartbeat(self) -> None:
        while True:
            await asyncio.sleep(self.heartbeat_seconds)
            if self._tick_pending:
                continue  # last tick not handled yet — don't pile up a stale batch
            self._tick_pending = True
            await self.queue.put(Tick())

    async def run(self) -> None:
        """Run until cancelled: channels + heartbeat feed one consumer."""
        producers = [asyncio.create_task(c.run(self._emit)) for c in self.channels]
        producers.append(asyncio.create_task(self._heartbeat()))
        producers.append(asyncio.create_task(self._control_server()))
        consumer = asyncio.create_task(self._consume())
        tasks = [*producers, consumer]
        _log.info("gateway up: %d channel(s)", len(self.channels))
        cancelled = False
        try:
            # Shield the children so cancelling run() (shutdown) doesn't reach
            # straight through gather and kill the consumer mid-drain — we stop
            # them deliberately, in order, in the finally.
            await asyncio.gather(*(asyncio.shield(t) for t in tasks))
        except asyncio.CancelledError:
            cancelled = True
        finally:
            with contextlib.suppress(FileNotFoundError, OSError):
                control_socket_path().unlink()  # don't leave a stale socket
            # Run the drain as its OWN shielded task so it's immune to this task's
            # cancellation — otherwise the drain await is itself cancelled and the
            # queued events are dropped (the very bug). Loop until it completes,
            # absorbing repeated cancellations; the drain is internally bounded.
            stopper = asyncio.ensure_future(self._drain_and_stop(producers, consumer))
            while not stopper.done():
                with contextlib.suppress(BaseException):
                    await asyncio.shield(stopper)
        if cancelled:
            raise asyncio.CancelledError  # preserve the shutdown contract for callers

    async def _drain_and_stop(
        self, producers: list[asyncio.Task], consumer: asyncio.Task
    ) -> None:
        """Graceful shutdown: stop producers (no new events), let the consumer
        finish what's already queued — a routine `service restart` must not drop
        the companion's in-flight or queued message — then stop the consumer. Each
        step is bounded so a stuck task can't wedge the exit."""
        for task in producers:
            task.cancel()
        with contextlib.suppress(Exception):
            await asyncio.wait_for(
                asyncio.gather(*producers, return_exceptions=True), timeout=5
            )
        with contextlib.suppress(Exception):
            await asyncio.wait_for(self.queue.join(), timeout=5)  # finish queued events
        consumer.cancel()
        with contextlib.suppress(Exception):
            await asyncio.wait_for(asyncio.gather(consumer, return_exceptions=True), timeout=5)
