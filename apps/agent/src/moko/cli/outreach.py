"""`moko outreach [--force]` — Moko's reach-out, on demand.

Plain `moko outreach` runs the *real* decision the heartbeat makes: Moko consults
itself (the time, the recent chat, its curiosities and any due reminders) and
either reaches out or **decides to wait, showing its reasoning**. It skips the
timing fuses (quiet hours, settle, the reconsider timer) so the brain's judgment
is what's exercised.

`--force` skips the deliberation entirely and just *sends* the best thing Moko has
(a due reminder, else its top open question), composed in voice — for when you
want a ping regardless of timing or what it would have decided.

Runs directly against the shared store, so it's fine with the daemon live — WAL +
busy_timeout serialize writes, so a command queues behind the daemon's writes
rather than racing them. (Its events go to the persisted Outreach tab, not the
daemon's live feed, since it's a separate process.)
"""

from __future__ import annotations

import asyncio
import os

import httpx

from ..agent import Brain
from ..agent.brain import OutreachProposal
from ..channels.telegram import TelegramChannel
from ..clock import now_utc
from ..config import config as cfg
from ..config import secrets
from ..config.paths import db_path, ensure_home
from ..llm import UnsupportedAuth, build_client
from ..store import Store
from ..theme import console


async def _pick(store) -> OutreachProposal | None:
    """The best candidate to raise, ignoring the timing governor (the --force
    path): a due reminder if any, else Moko's top open question."""
    due = await store.due_commitments(now_utc().isoformat())
    if due:
        return OutreachProposal(
            "commitment",
            "; ".join(c.description for c in due),
            counts_to_budget=False,
            commitment_ids=[c.id for c in due],
        )
    gap = await store.next_curiosity_gap()
    if gap is not None:
        return OutreachProposal("curiosity", gap.description, counts_to_budget=True, gap_id=gap.id)
    return None


async def _channel(config, http) -> TelegramChannel:
    return TelegramChannel(
        os.environ.get("MOKO_TELEGRAM_TOKEN") or secrets.get_secret("telegram_bot_token"),
        config.telegram_chat_id, client=http, allowed_sender_id=config.telegram_user_id,
    )


def _read_photo(proposal: OutreachProposal) -> tuple[bytes, str] | None:
    """Read the photo a "show" outreach wants to send, or None (no photo, or it couldn't
    be read → the outreach degrades to text rather than failing). Mirrors the daemon's
    `gateway._outreach_photo` — the in-process path must send photos too, not silently
    drop them."""
    if not proposal.photo_path:
        return None
    from pathlib import Path

    from ..config.paths import media_dir
    try:
        return (media_dir() / proposal.photo_path).read_bytes(), Path(proposal.photo_path).name
    except OSError:
        return None


async def _deliver(brain, channel, text: str, proposal: OutreachProposal) -> None:
    """Send an outreach and record it — the in-process twin of the daemon's
    `gateway._deliver_outreach`. A "show" proposal carries a photo, sent as a captioned
    image where the channel can upload files (with a plain-text fallback), and the sent
    text rides into `record_outreach` so Moko's own history shows what it said (without
    it, the next consult can't see the message and re-sends — the morning-nagging bug)."""
    photo = _read_photo(proposal)
    sent_file = False
    if photo is not None and getattr(channel, "can_send_files", False):
        data, name = photo
        try:
            await channel.send_file(data, filename=name, caption=text, kind="image")
            sent_file = True
        except Exception:  # noqa: BLE001 — a failed upload degrades to plain text, never drops
            sent_file = False
    if not sent_file:
        await channel.send(text)
    await brain.record_outreach(proposal, channel.name, text=text)


async def _force_send(brain, store, config, http) -> int:
    """Just send the best candidate — no deliberation (the old `nudge`)."""
    proposal = await _pick(store)
    if proposal is None:
        console.print(
            "[moko]Moko has nothing to reach out about yet.[/moko] "
            "Chat a little so it has a curiosity or a reminder to raise."
        )
        return 0
    channel = await _channel(config, http)
    text = await brain.phrase_outreach(proposal)
    await _deliver(brain, channel, text, proposal)
    console.print(f"[moko]Sent on Telegram:[/moko] {text}")
    return 0


async def _deliberate(brain, store, config, http) -> int:
    """Run the real decision; send only if Moko decides to, and show its reasoning."""
    console.print("[accent]Moko is weighing whether to reach out…[/accent] 🌸")
    decision = await brain.consider_outreach(brain.clock.now(), force=True)
    if decision is not None:
        text, proposal = decision
        channel = await _channel(config, http)
        await _deliver(brain, channel, text, proposal)
        console.print(f"[moko]Reached out on Telegram:[/moko] {text}")
        return 0
    # It chose NOT to act — surface the deliberation it just recorded so the
    # choice (and why) is visible, the whole point of the on-demand trigger.
    consults = await store.recent_outreach_decisions(limit=1)
    if consults and consults[0]["reasoning"]:
        c = consults[0]
        console.print(f"[moko]Moko decided to hold off.[/moko] {c['reasoning']}")
        if c["reconsider_minutes"]:
            console.print(f"[dim]It'll reconsider in ~{round(c['reconsider_minutes'])}m.[/dim]")
    else:
        console.print(
            "[moko]Nothing to reach out about yet.[/moko] "
            "Chat a little so Moko has a curiosity or a reminder to raise."
        )
    return 0


async def _amain(config, client, *, force: bool) -> int:
    ensure_home()
    store = await Store.open(db_path())
    http = httpx.AsyncClient()
    try:
        brain = Brain(store, client, config)
        return await (_force_send if force else _deliberate)(brain, store, config, http)
    finally:
        await http.aclose()
        await store.close()


def run(*, force: bool = False) -> int:
    from . import _daemon
    from ._common import refuse_if_container_daemon_down

    # Let the living daemon do it (it sends via its own channel + shows in the feed).
    handled = _daemon.try_command("outreach_force" if force else "outreach")
    if handled is not None:
        return handled
    # No daemon reachable. In a container tier, refuse rather than run a second mind.
    config = cfg.load()
    if config is not None and refuse_if_container_daemon_down(config):
        return 1
    return _in_process(force=force)


def _in_process(*, force: bool = False) -> int:
    config = cfg.load()
    if config is None:
        console.print("We haven't met yet — run [accent]moko setup[/accent] first. 🌸")
        return 0
    if not config.telegram_user_id or not config.telegram_chat_id:
        console.print("[moko]Connect Telegram first:[/moko] run [accent]moko pair[/accent].")
        return 1
    if not (os.environ.get("MOKO_TELEGRAM_TOKEN") or secrets.get_secret("telegram_bot_token")):
        console.print("[moko]No Telegram token yet — run [accent]moko pair[/accent].[/moko]")
        return 1
    try:
        client = build_client(config)
    except UnsupportedAuth as exc:
        console.print(f"[moko]{exc}[/moko]")
        return 1
    if client is None:
        console.print("[moko]My brain isn't chosen yet — run [accent]moko setup[/accent].[/moko]")
        return 1
    return asyncio.run(_amain(config, client, force=force))
