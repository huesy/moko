"""The channel contract.

A channel is two things: a source of inbound messages and a way to send.
`run()` receives until cancelled, handing each message to the gateway via
the `emit` callback; `send()` pushes Moko's reply (or an unprompted
message) to the companion.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field


@dataclass
class Attachment:
    """A file that rode in on a message — already downloaded by the channel, so the
    bytes are in hand. The channel is pure transport; the store persists it (writes
    the blob, records a sourced row). `ref` is the channel handle to re-fetch it."""

    kind: str  # image | audio | video | document
    data: bytes
    mime: str | None = None
    filename: str | None = None
    caption: str | None = None
    ref: str = ""  # channel-specific id (e.g. a Telegram file_id)


@dataclass
class Inbound:
    """One received message, channel-neutral."""

    text: str
    sender_id: str  # channel-specific id of who sent it (for access control)
    chat_id: str  # where a reply goes
    channel: str  # the channel's name
    attachments: list[Attachment] = field(default_factory=list)  # files sent with it


class Channel(ABC):
    name: str
    can_send_files: bool = False  # does this channel support send_file()? (the CLI doesn't)

    @abstractmethod
    async def run(self, emit: Callable[[Inbound], Awaitable[None]]) -> None:
        """Receive messages until cancelled, calling `await emit(inbound)`
        for each. Should tolerate transient errors and keep going."""

    @abstractmethod
    async def send(self, text: str) -> None:
        """Send a message to the companion on this channel."""

    async def send_file(
        self, data: bytes, *, filename: str, caption: str | None = None, kind: str = "document"
    ) -> None:
        """Send a file to the companion. Default: unsupported — channels that can
        (Telegram) override and set `can_send_files = True`. The gateway only wires
        the send path on a capable channel, so this raising is a backstop."""
        raise NotImplementedError("this channel can't send files")

    async def typing(self) -> None:
        """Signal that Moko is composing a reply (e.g. Telegram's "typing…").
        Best-effort and optional — the default does nothing, so channels
        without the notion (the CLI) simply ignore it."""
        return None

    async def request_approval(self, description: str, *, always_ok: bool = True) -> str | None:
        """Ask the companion to approve a sensitive action, returning a handle to
        the prompt message (so it can be cleared on resolution) or None if the
        channel has no such handle. The default is a text prompt they reply to
        ('yes'/'yes always'/'no'); channels with native UI (e.g. Telegram inline
        buttons) override this. Either way the answer comes back as an ordinary
        inbound the gateway resolves deterministically.

        `always_ok` is False for escalations (sudo/system, sending data off-box,
        deleting outside the workspace) — those can't be blanket-trusted, so the
        'always' affordance is withheld."""
        always = " (or 'yes always' to trust this kind)" if always_ok else ""
        await self.send(
            f"🔐 Approve: {description}\n"
            f"Reply 'yes' to go ahead{always}, or 'no'."
        )
        return None

    async def clear_approval(self, handle: str) -> None:
        """Tidy a resolved approval prompt (e.g. strip its inline buttons), given
        the handle `request_approval` returned. Best-effort; the default does
        nothing (a text prompt has nothing to clear)."""
        return None
