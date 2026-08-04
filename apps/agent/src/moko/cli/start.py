"""`moko start` — bring Moko online as the always-on daemon.

Boots the gateway with the Telegram channel and runs until stopped
(Ctrl+C, or systemd stopping the service). Everything funnels through the
single serialized event loop in `gateway.py`.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import sys

import httpx

from ..agent import Brain
from ..channels.telegram import TelegramChannel
from ..config import config as cfg
from ..config import secrets
from ..config.paths import db_path, ensure_home
from ..gateway import Gateway
from ..llm import UnsupportedAuth, build_client
from ..store import Store
from ..theme import console

# Friendly names for the online message.
_CHANNEL_LABELS = {"telegram": "Telegram", "cli": "this terminal"}


async def _serve(config, llm_client, token: str) -> None:
    ensure_home()
    store = await Store.open(db_path())
    http = httpx.AsyncClient()
    monitor_runner = None  # set below; referenced in finally even on early failure
    monitor_store = None  # the monitor's OWN read-only connection (see below)
    try:
        brain = Brain(store, llm_client, config)
        # Bring up every available channel — no flags. Telegram if paired;
        # the terminal if we're attached to one (so a systemd service, with
        # no tty, runs Telegram only). New channels join this list.
        channels = [
            TelegramChannel(
                token,
                config.telegram_chat_id,
                client=http,
                allowed_sender_id=config.telegram_user_id,
            )
        ]
        if sys.stdin.isatty():
            from ..channels.cli import CliChannel

            channels.append(CliChannel())

        gateway = Gateway(store, brain, channels)

        # The monitor observes the event bus and serves the dashboard. Its reach
        # follows the configured access tier (local by default); anything beyond
        # localhost is password-protected. Best-effort: if it can't bind, Moko
        # still runs.
        try:
            from ..monitor import access as monitor_access
            from ..monitor import serve as serve_monitor
            from ..monitor.server import DEFAULT_PORT

            tier = config.monitor_access
            host, problem = monitor_access.resolve_host(tier)
            monitor_token = (
                monitor_access.ensure_token() if monitor_access.requires_auth(tier) else ""
            )
            if problem:  # e.g. Tailscale requested but not up — we fell back to local
                tier = "local"
                console.print(f"[muted]{problem}[/muted]")
            # The monitor is a pure observer: give it its OWN read-only, WAL
            # snapshot-isolated connection instead of the gateway's read-write
            # Store, so "read-only" is structural, not merely asserted — the
            # monitor cannot mutate the mind or contend with the serial consumer.
            monitor_store = await Store.open_readonly(db_path())
            monitor_runner = await serve_monitor(
                monitor_store, config, host=host, token=monitor_token
            )
        except Exception:  # noqa: BLE001 — the monitor must never block coming online
            # Best-effort, but never *silent*: a swallowed startup error with no trace is
            # why "the monitor won't come up" is undiagnosable. Log the reason (port already
            # in use, unbuilt SPA, etc.) so `moko service logs` shows it.
            from ..log import get_logger

            get_logger("monitor").warning(
                "monitor failed to start (continuing without it)", exc_info=True
            )
            console.print(
                "[muted]Monitor didn't start (continuing without it) — "
                "see [accent]moko service logs[/accent].[/muted]"
            )

        task = asyncio.create_task(gateway.run())
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, task.cancel)

        where = " and ".join(_CHANNEL_LABELS.get(c.name, c.name) for c in channels)
        console.print(
            f"[moko]🌸 Online and listening on {where}.[/moko] [muted](Ctrl+C to stop)[/muted]"
        )
        if monitor_runner is not None:
            url = monitor_access.reachable_url(tier, DEFAULT_PORT)
            lock = " 🔒 password-protected" if monitor_token else ""
            console.print(
                f"[muted]Monitor ({monitor_access.TIER_LABELS.get(tier, tier)}): "
                f"[accent]{url}[/accent]{lock}[/muted]"
            )
        with contextlib.suppress(asyncio.CancelledError):
            await task
    finally:
        # Bound every teardown step. The monitor's SSE streams (an open dashboard
        # tab) never end on their own, so aiohttp's graceful cleanup would wait —
        # unbounded, that hung shutdown until systemd's SIGKILL. Capping each step
        # lets the process exit in a couple of seconds AND still close the store
        # cleanly (WAL checkpoint), rather than being killed mid-flight.
        async def _bounded(coro, secs: float) -> None:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(coro, timeout=secs)

        if monitor_runner is not None:
            await _bounded(monitor_runner.cleanup(), 2)
        if monitor_store is not None:
            await _bounded(monitor_store.close(), 2)
        await _bounded(http.aclose(), 2)
        await _bounded(store.close(), 3)


def run() -> int:
    config = cfg.load()
    if config is None:
        console.print("We haven't met yet — run [accent]moko setup[/accent] first. 🌸")
        return 0
    if not config.telegram_user_id or not config.telegram_chat_id:
        console.print("[moko]Let's connect Telegram first:[/moko] run [accent]moko pair[/accent].")
        return 1

    token = os.environ.get("MOKO_TELEGRAM_TOKEN") or secrets.get_secret("telegram_bot_token")
    if not token:
        console.print("[moko]No Telegram token yet — run [accent]moko pair[/accent].[/moko]")
        return 1

    try:
        llm_client = build_client(config)
    except UnsupportedAuth as exc:
        console.print(f"[moko]{exc}[/moko]")
        return 1
    if llm_client is None:
        console.print("[moko]My brain isn't chosen yet — run [accent]moko setup[/accent].[/moko]")
        return 0

    console.print("[accent]Moko 🌸 is coming online…[/accent]")
    asyncio.run(_serve(config, llm_client, token))
    console.print("[moko]I've gone to rest. Bye for now![/moko]")
    return 0
