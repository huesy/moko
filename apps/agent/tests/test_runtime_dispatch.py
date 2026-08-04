"""The dual-writer guard (SANDBOX.md): a control command must never spin up a
second in-process mind against the shared DB when a daemon is present-but-slow,
and in a container tier it must refuse to fall back at all.

Two layers:
  - `_dispatch` correctly separates OK / BUSY / UNREACHABLE against real sockets.
  - the fallback logic (`run_via_daemon_or`) refuses in container mode, falls back
    on host — proving `amain` is never invoked in-process behind a live sandbox.
"""

from __future__ import annotations

import asyncio
import contextlib

from moko.cli import _common, _daemon
from moko.config.config import Config, RuntimeConfig
from moko.config.paths import control_socket_path, ensure_home


def _make_config(mode: str = "host") -> Config:
    c = Config(
        companion_name="A", timezone="UTC", sleep_start="23:00", sleep_end="07:00",
        proactivity="moderate",
    )
    c.runtime = RuntimeConfig(mode=mode)
    c.save()
    return c


async def _serve(handler):
    ensure_home()
    return await asyncio.start_unix_server(handler, path=str(control_socket_path()))


# ---- _dispatch: OK / BUSY / UNREACHABLE -------------------------------------


async def test_dispatch_ok_when_daemon_replies():
    async def handler(reader, writer):
        await reader.readline()
        writer.write(b"did the thing\n")
        await writer.drain()
        writer.close()

    server = await _serve(handler)
    try:
        outcome, payload = await _daemon._dispatch("dream")
        assert outcome == _daemon._OK
        assert payload == "did the thing"
    finally:
        server.close()
        await server.wait_closed()


async def test_dispatch_busy_when_daemon_connects_but_never_replies(monkeypatch):
    # A daemon that accepts the connection but is slow past the read timeout is ALIVE:
    # BUSY, not UNREACHABLE — the caller must not double-run.
    monkeypatch.setattr(_daemon, "_READ_TIMEOUT", 0.2)
    release = asyncio.Event()  # let the handler exit promptly at teardown

    async def handler(reader, writer):
        await reader.readline()
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(release.wait(), timeout=3)  # doesn't reply in time
        writer.close()

    server = await _serve(handler)
    try:
        outcome, _ = await _daemon._dispatch("dream")
        assert outcome == _daemon._BUSY
    finally:
        release.set()
        server.close()
        # Py3.12's wait_closed() waits on live connections; bound it so a lingering
        # handler can never hang the suite.
        with contextlib.suppress(Exception):
            await asyncio.wait_for(server.wait_closed(), timeout=1)


async def test_dispatch_unreachable_when_no_socket():
    outcome, _ = await _daemon._dispatch("dream")
    assert outcome == _daemon._UNREACHABLE


async def test_dispatch_unreachable_when_socket_is_stale():
    # A leftover file at the socket path (a crashed daemon, or a subuid-owned socket
    # we can't open) is UNREACHABLE, not a live daemon.
    ensure_home()
    control_socket_path().write_text("not a socket")
    outcome, _ = await _daemon._dispatch("dream")
    assert outcome == _daemon._UNREACHABLE


# ---- try_command: BUSY returns 0 (no fallback), UNREACHABLE returns None -----


def test_try_command_busy_returns_zero_not_none(monkeypatch):
    async def fake(_action):
        return (_daemon._BUSY, None)

    monkeypatch.setattr(_daemon, "_dispatch", fake)
    # 0, not None → the caller treats it as handled and never falls back.
    assert _daemon.try_command("dream") == 0


def test_try_command_unreachable_returns_none(monkeypatch):
    async def fake(_action):
        return (_daemon._UNREACHABLE, None)

    monkeypatch.setattr(_daemon, "_dispatch", fake)
    assert _daemon.try_command("dream") is None


# ---- the fallback decision: refuse in container, fall back on host -----------


def _amain_recorder(flag: dict):
    async def amain(_config, _client):
        flag["ran"] = True
        return 0

    return amain


def test_host_mode_falls_back_in_process(monkeypatch):
    _make_config(mode="host")
    monkeypatch.setattr("moko.cli._daemon.try_command", lambda _a: None)
    monkeypatch.setattr(_common, "build_brain_or_hint", lambda _c: object())
    flag: dict = {}
    rc = _common.run_via_daemon_or("dream", _amain_recorder(flag))
    assert rc == 0
    assert flag.get("ran") is True  # host mode: in-process fallback is correct


def test_container_mode_refuses_fallback(monkeypatch):
    _make_config(mode="podman")
    monkeypatch.setattr("moko.cli._daemon.try_command", lambda _a: None)
    monkeypatch.setattr(_common, "build_brain_or_hint", lambda _c: object())
    flag: dict = {}
    rc = _common.run_via_daemon_or("dream", _amain_recorder(flag))
    assert rc == 1  # refused
    assert "ran" not in flag  # the second mind never started


def test_container_refusal_helper():
    host = Config(companion_name="A", timezone="UTC", sleep_start="23:00",
                  sleep_end="07:00", proactivity="moderate")
    host.runtime = RuntimeConfig(mode="host")
    assert _common.refuse_if_container_daemon_down(host) is False
    for mode in ("docker", "podman"):
        c = Config(companion_name="A", timezone="UTC", sleep_start="23:00",
                   sleep_end="07:00", proactivity="moderate")
        c.runtime = RuntimeConfig(mode=mode)
        assert _common.refuse_if_container_daemon_down(c) is True
