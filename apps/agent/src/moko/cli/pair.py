"""`moko pair` — connect Moko to your Telegram so it can talk to you.

You make a bot with @BotFather, paste its token, then send the bot a
message; Moko binds to *you* (your Telegram id) and no one else.
"""

from __future__ import annotations

import asyncio
import contextlib
import os

import httpx
import questionary

from ..channels import telegram
from ..config import config as cfg
from ..config import secrets
from ..theme import QUESTIONARY_STYLE, console


async def _pair(token: str, code: str) -> dict | None:
    async with httpx.AsyncClient() as client:
        try:
            info = await telegram.wait_for_first_message(client, token, expected_code=code)
        except httpx.HTTPError as exc:
            console.print(f"[moko]Couldn't reach Telegram: {exc}[/moko]")
            return None
        if info is not None:
            # Resume the daemon's polling past the code we just consumed, so it
            # doesn't re-deliver the pairing code as a message and reply to it.
            telegram.save_offset(info["offset"])
            # Binding succeeded even if the hello fails to send.
            with contextlib.suppress(httpx.HTTPError):
                await telegram._send_message(
                    client, token, info["chat_id"], "Hi! We're connected. 🌸"
                )
        return info


def setup_telegram(config: cfg.Config) -> bool:
    """Connect Telegram: take the bot token, wait for the companion's first
    message, and bind to them alone. Mutates and saves `config`; returns whether
    it connected. Shared by `moko pair` and the setup wizard."""
    # Pairing needs exclusive access to the bot's getUpdates stream. If the
    # daemon is running it's already polling — two pollers on one token make
    # Telegram 409, and the daemon would overwrite the offset we save. Stop
    # first, pair, then start, so this stays a clean single-poller operation.
    from . import service

    if service.service_state().get("active"):
        console.print(
            "[moko]Moko's service is running and already watching Telegram.[/moko]\n"
            "[muted]Stop it first so pairing has a clear line:[/muted]\n"
            "  [accent]moko service stop[/accent]   (then re-run this, and "
            "[accent]moko service start[/accent] after)"
        )
        return False

    token = os.environ.get("MOKO_TELEGRAM_TOKEN") or secrets.get_secret("telegram_bot_token")
    if not token:
        console.print(
            "[muted]Make a bot with @BotFather on Telegram (send it /newbot) and paste\n"
            "the token it gives you. It's stored only on this machine.[/muted]"
        )
        token = questionary.password(
            "Telegram bot token:",
            style=QUESTIONARY_STYLE,
            validate=lambda t: bool(t.strip()) or "A token, please :)",
        ).ask()
        if not token or not token.strip():
            return False
        token = token.strip()
    secrets.set_secret("telegram_bot_token", token)

    # Bind to whoever sends this one-time code — not just whoever messages
    # first, so a stranger who finds the bot can't pair themselves.
    code = telegram.make_pairing_code()
    console.print("\n[accent]Now open Telegram, find your bot, and send it this code:[/accent]")
    console.print(f"\n      [moko]{code}[/moko]\n")
    console.print(
        "[muted]Only the message with this exact code will pair me — "
        "send nothing else. (Ctrl+C to skip.)[/muted]"
    )
    try:
        info = asyncio.run(_pair(token, code))
    except KeyboardInterrupt:
        console.print("\n[moko]Skipped — connect any time with [accent]moko pair[/accent].[/moko]")
        return False

    if info is None:
        console.print(
            "[moko]I didn't hear anything. Message the bot, then run "
            "[accent]moko pair[/accent] again.[/moko]"
        )
        return False

    config.telegram_user_id = info["user_id"]
    config.telegram_chat_id = info["chat_id"]
    config.save()
    console.print(f"[moko]Paired with {info['name']}! 🌸[/moko]")
    return True


def run() -> int:
    config = cfg.load()
    if config is None:
        console.print("We haven't met yet — run [accent]moko setup[/accent] first. 🌸")
        return 0
    if setup_telegram(config):
        console.print("Run [accent]moko start[/accent] to bring me online. 🌸")
        return 0
    return 1
