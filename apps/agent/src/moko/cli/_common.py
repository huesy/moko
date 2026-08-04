"""Shared CLI preflight: load config, build the brain, and the daemon-or-in-process shape.

Several commands open with the same dance — find the config (or nudge to `moko setup`), build
the LLM client (or nudge), and either hand off to a running daemon or run in-process.
reflect/dream/research run entirely through `run_via_daemon_or`; the building blocks
(`load_config_or_hint`, `build_brain_or_hint`) are here for any other command that wants the
same wording, each picking its own exit code.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

from ..config import config as cfg
from ..llm import LLMClient, UnsupportedAuth, build_client
from ..theme import console


def load_config_or_hint() -> cfg.Config | None:
    """The loaded config, or None after nudging an unconfigured companion to `moko setup`."""
    config = cfg.load()
    if config is None:
        console.print("We haven't met yet — run [accent]moko setup[/accent] first. 🌸")
    return config


def build_brain_or_hint(config: cfg.Config) -> LLMClient | None:
    """The configured LLM client, or None after printing why it's unavailable (an unsupported
    sign-in, or no brain chosen yet). The caller picks the exit code — some commands treat a
    missing brain as a soft 0, others as a hard 1."""
    try:
        client = build_client(config)
    except UnsupportedAuth as exc:
        console.print(f"[moko]{exc}[/moko]")
        return None
    if client is None:
        console.print("[moko]My brain isn't chosen yet — run [accent]moko setup[/accent].[/moko]")
    return client


def refuse_if_container_daemon_down(config: cfg.Config) -> bool:
    """In a container tier the daemon *is* the runtime — a control command that can't
    reach it must NOT run in this process, or it spins up a second mind writing the
    shared, bind-mounted DB (duplicate dreams, double outreach, racing integrity).
    Print how to bring the sandbox up and return True (caller should stop). Host mode
    returns False — falling back in-process there is correct."""
    if not config.runtime.expects_daemon:
        return False
    console.print(
        "[moko]Moko's sandbox isn't responding.[/moko] "
        "Bring it up with [accent]moko service start[/accent], "
        "or check [accent]moko service status[/accent]."
    )
    return True


def run_via_daemon_or(
    action: str, amain: Callable[[cfg.Config, LLMClient], Awaitable[int]]
) -> int:
    """The shape of `moko reflect|dream|research`: hand the action to the living daemon if it's
    running (so the work shows up in the feed), else run `amain(config, client)` in-process
    against the shared store. In a container tier we refuse rather than fall back — see
    `refuse_if_container_daemon_down`."""
    from . import _daemon

    handled = _daemon.try_command(action)
    if handled is not None:
        return handled
    config = load_config_or_hint()
    if config is None:
        return 0
    if refuse_if_container_daemon_down(config):
        return 1
    client = build_brain_or_hint(config)
    if client is None:
        return 1
    return asyncio.run(amain(config, client))
