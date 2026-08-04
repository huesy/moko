"""The monitor — a local, read-only window into a running Moko.

A small HTTP server (bound to localhost) that serves the monitor SPA and two
endpoints: a status snapshot from the store, and an SSE stream of the live
event bus. It is one *observer* among many — see :mod:`moko.events`.
"""

from .server import build_app, serve

__all__ = ["build_app", "serve"]
