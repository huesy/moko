"""Unit generation for the host runtime (the systemd interaction itself is
exercised manually — it needs a real user bus). The unit text now lives in
`moko.runtime.host`; `moko.cli.service` is a thin facade over `get_runtime`.
"""

from moko.config.config import Config, RuntimeConfig
from moko.config.paths import moko_home
from moko.runtime import HostRuntime, get_runtime, host


def _config(hardened: str = "off", mounts: list[str] | None = None) -> Config:
    c = Config(
        companion_name="A", timezone="UTC", sleep_start="23:00", sleep_end="07:00",
        proactivity="moderate",
    )
    c.runtime = RuntimeConfig(mode="host", hardened=hardened, mounts=mounts or [])
    return c


def test_unit_runs_moko_start_and_restarts():
    text = host._unit_text()
    assert "moko start" in text  # ExecStart launches the daemon
    assert "Restart=on-failure" in text
    assert text.rstrip().endswith("WantedBy=default.target")  # user-session target


def test_moko_bin_resolves_to_an_entry_point():
    assert host._moko_bin().endswith("moko")


def test_plain_unit_has_no_hardening():
    text = host._unit_text(_config(hardened="off"))
    assert "NoNewPrivileges" not in text
    assert "ProtectSystem" not in text


def test_hardened_on_protects_system_but_keeps_home_reachable():
    text = host._unit_text(_config(hardened="on"))
    assert "NoNewPrivileges=yes" in text
    assert "ProtectSystem=strict" in text
    assert f"ReadWritePaths={moko_home()}" in text  # Moko's home is granted back
    assert "ProtectHome=tmpfs" not in text  # $HOME stays reachable (M7 intact)
    # Still a valid, restartable user unit.
    assert "moko start" in text
    assert text.rstrip().endswith("WantedBy=default.target")


def test_hardened_strict_hides_home_and_grants_mounts():
    text = host._unit_text(_config(hardened="strict", mounts=["/home/x/proj"]))
    assert "ProtectHome=tmpfs" in text
    assert f"ReadWritePaths={moko_home()}" in text
    assert "ReadWritePaths=/home/x/proj" in text  # companion's chosen M7 reach


def test_get_runtime_defaults_to_host():
    assert isinstance(get_runtime(None), HostRuntime)
    assert isinstance(get_runtime(_config()), HostRuntime)


# ── control-socket exit codes (the `moko <cmd>` CLI ↔ daemon status byte) ──
def test_dispatch_splits_the_status_prefix(monkeypatch):
    # The daemon prefixes its reply with '+' (ok) or '-' (the action failed). _dispatch
    # peels that byte off into the outcome so `moko dream && …` sees the real result.
    import asyncio

    from moko.cli import _daemon

    class _Reader:
        def __init__(self, payload):
            self._payload = payload

        async def read(self):
            return self._payload

    class _Writer:
        def write(self, _data):
            pass

        async def drain(self):
            pass

        def close(self):
            pass

        async def wait_closed(self):
            pass

    def run_with_reply(reply_bytes):
        async def open_conn(_path):
            return _Reader(reply_bytes), _Writer()

        monkeypatch.setattr(_daemon, "control_socket_path", lambda: _ExistingPath())
        monkeypatch.setattr(_daemon.asyncio, "open_unix_connection", open_conn)
        return asyncio.run(_daemon._dispatch("dream"))

    ok = run_with_reply(b"+Dreamt - consolidated 3 things.\n")
    assert ok == (_daemon._OK, "Dreamt - consolidated 3 things.")
    err = run_with_reply(b"-That didn't work: boom\n")
    assert err == (_daemon._ERR, "That didn't work: boom")
    # An unprefixed line (an older daemon) is treated as success, not swallowed.
    legacy = run_with_reply(b"Dreamt.\n")
    assert legacy == (_daemon._OK, "Dreamt.")


class _ExistingPath:
    """A stand-in control-socket path that always reports it exists."""

    def exists(self):
        return True

    def __str__(self):
        return "/nonexistent/control.sock"


def test_try_command_exit_codes(monkeypatch):
    # _OK → 0, _ERR → 1 (so a scripted chain stops), _BUSY → 0 (daemon owns it),
    # _UNREACHABLE → None (caller decides on an in-process fallback).
    from moko.cli import _daemon

    def with_outcome(outcome, payload):
        async def fake_dispatch(_action):
            return (outcome, payload)

        monkeypatch.setattr(_daemon, "_dispatch", fake_dispatch)
        return _daemon.try_command("dream")

    assert with_outcome(_daemon._OK, "done") == 0
    assert with_outcome(_daemon._ERR, "That didn't work: boom") == 1
    assert with_outcome(_daemon._BUSY, None) == 0
    assert with_outcome(_daemon._UNREACHABLE, None) is None
