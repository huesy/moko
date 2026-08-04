"""`moko chat` — talk to Moko in the terminal.

A native-scrollback REPL on Moko's own `tui` framework: the conversation prints
as ordinary terminal text (scrolls, selects, copies like any output), while a
multi-line composer and a status bar render in place. Enter sends; Ctrl+J — or
a trailing ``\\`` then Enter, or Shift+Enter on kitty-capable terminals — adds a
line; the composer grows as you type. Conversation and sessions live in the
database, so the chat survives restarts; all the real logic is in `agent.brain`.
"""

from __future__ import annotations

import asyncio
import contextlib

from rich.markup import escape

from ..agent import Brain
from ..config import config as cfg
from ..config.paths import db_path, ensure_home
from ..llm import UnsupportedAuth, build_client
from ..store import Store
from ..theme import MOKO_PINK, console
from ..tui import Prompt
from ..tui.prompt import dim, paint
from ._daemon import is_daemon_live

YOU_GREY = "#b9b9b9"
_LEVELS = cfg.CHAT_VERBOSITY
_SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"


def _fmt_tool(ev: dict) -> str:
    """A compact one-line trace of a tool call (verbose mode), as rich markup."""
    tool = ev.get("tool")
    args = ev.get("args") or {}
    result = str(ev.get("result") or "")
    if tool == "recall":
        n = len([ln for ln in result.splitlines() if ln.strip()])
        return f"[dim]· recall '{escape(str(args.get('query', '')))}' → {n} hit(s)[/dim]"
    if tool == "lookup":
        head = result.splitlines()[0] if result.strip() else "nothing"
        return f"[dim]· lookup '{escape(str(args.get('name', '')))}' → {escape(head)}[/dim]"
    if tool == "remember":
        parts = [
            f"{len(args[k])} {k}"
            for k in ("subjects", "attributes", "relationships", "gaps", "thoughts")
            if isinstance(args.get(k), list) and args.get(k)
        ]
        if args.get("speaker_sentiment"):
            parts.append("sentiment")
        return f"[dim]· remember ({', '.join(parts) or 'nothing'})[/dim]"
    return f"[dim]· {escape(str(tool))}[/dim]"


def _fmt_stats(turn) -> str:
    rec = "✓" if turn.recorded else "—"
    return (
        f"[dim]· in {turn.input_tokens} · out {turn.output_tokens}"
        f" · cache {turn.cache_read_tokens or 0} · recorded {rec}[/dim]"
    )


def _status(level: str) -> str:
    """The bottom status bar: shortcuts on the left, current mode on the right."""
    left = dim("⏎ send · ⌃J newline · /verbose /new /exit")
    return f"{left}   {dim(level)}"


def _thinking(frame: str) -> str:
    """The transient `moko › ⠋ thinking…` line shown while Moko composes."""
    return paint("moko ›", MOKO_PINK) + dim(f" {frame} thinking…")


async def _spin(prompt: Prompt) -> None:
    i = 0
    while True:
        prompt.set_pending(_thinking(_SPINNER[i % len(_SPINNER)]))
        i += 1
        await asyncio.sleep(0.1)


async def _stop(task: asyncio.Task) -> None:
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


def _refuse_if_daemon_live(config) -> bool:
    """A live daemon already owns the DB. `moko chat` builds its *own* Brain, so
    opening the store here would put two minds writing the same SQLite file — the
    dual-writer hazard SANDBOX.md closed for containers but left open on the host.

    Container tier: refuse (the daemon *is* the runtime — a second mind races the
    bind-mounted DB). Host tier: warn loudly and proceed, since a curious human may
    knowingly accept the risk. Returns True iff chat should abort."""
    if not is_daemon_live():
        return False
    console.print(
        "\n[moko]⚠ Moko is already awake as a background service.[/moko]\n"
        "  It's holding the memory store open right now, so a second chat mind\n"
        "  would write the same database at the same time (dual-writer).\n"
        "  Instead:\n"
        "    • talk to Moko on Telegram (it's already listening there), or\n"
        "    • stop the service first: [accent]moko service stop[/accent]\n"
    )
    if config.runtime.expects_daemon:
        console.print(
            "[moko]In this sandboxed tier the daemon owns the store — refusing.[/moko]\n"
        )
        return True
    console.print(
        "[muted]On the host I'll continue anyway, but two minds are now sharing "
        "the store.[/muted]\n"
    )
    return False


async def _amain(config) -> int:
    ensure_home()
    if _refuse_if_daemon_live(config):
        return 1
    store = await Store.open(db_path())
    try:
        try:
            client = build_client(config)
        except UnsupportedAuth as exc:
            console.print(f"[moko]{exc}[/moko]")
            return 1
        if client is None:
            console.print(
                "[moko]My brain isn't chosen yet.[/moko] Run [accent]moko setup[/accent] first. 🌸"
            )
            return 0

        brain = Brain(store, client, config)
        name = config.companion_name or "you"
        level = config.chat_verbosity if config.chat_verbosity in _LEVELS else "normal"

        async with Prompt(prompt="you › ", prompt_color=YOU_GREY) as prompt:
            prompt.emit(f"[bold {MOKO_PINK}]Moko 🌸[/]  [dim](chatting as {name})[/dim]")
            prompt.set_status(_status(level))

            def on_tool(ev: dict) -> None:
                if level == "verbose" and ev.get("type") == "tool":
                    prompt.emit(_fmt_tool(ev))

            while True:
                line = await prompt.read()
                if line is None:  # Ctrl+C / Ctrl+D
                    break
                text = line.strip()
                if not text:
                    continue
                if text in ("/exit", "/quit"):
                    break
                if text.startswith("/") and text[1:] in _LEVELS:
                    level = text[1:]
                    config.chat_verbosity = level
                    config.save()
                    prompt.set_status(_status(level))
                    prompt.emit(f"[{MOKO_PINK}]· {level} mode ·[/]")
                    continue
                if text == "/new":
                    await brain.new_session("user")
                    prompt.emit(f"[{MOKO_PINK}]· fresh session ·[/]")
                    continue

                prompt.emit(f"[{YOU_GREY}]you ›[/] {escape(text)}")
                spin = asyncio.create_task(_spin(prompt))  # "moko › ⠋ thinking…"
                try:
                    turn = await brain.respond(text, on_event=on_tool)
                except Exception as exc:  # keep the chat alive on a bad turn
                    await _stop(spin)
                    prompt.set_pending("")
                    prompt.emit(f"[red]…something went wrong: {escape(str(exc))}[/]")
                    continue
                await _stop(spin)
                prompt.set_pending("")
                prompt.emit(f"[bold {MOKO_PINK}]moko ›[/] {escape(turn.reply)}")
                if level == "verbose":
                    prompt.emit(_fmt_stats(turn))
    finally:
        await store.close()
    console.print("[moko]Bye for now![/moko]")
    return 0


def run() -> int:
    config = cfg.load()
    if config is None:
        console.print("We haven't met yet — run [accent]moko setup[/accent] first. 🌸")
        return 0
    return asyncio.run(_amain(config))
