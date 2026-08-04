"""Channels — the surfaces Moko talks through (Telegram, CLI, …).

A channel receives messages (emitting `Inbound` events to the gateway) and
sends Moko's words back. It holds no policy or memory — that's the gateway
and the store.
"""

from .base import Channel, Inbound

__all__ = ["Channel", "Inbound"]
