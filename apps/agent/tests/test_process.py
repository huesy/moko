"""The background process supervisor: start, track, capture logs, list, stop."""

import asyncio

import pytest

from moko.tools.process import ProcessSupervisor


@pytest.fixture
def sup(tmp_path, monkeypatch):
    # Isolate the registry + logs + work dir under a temp MOKO_HOME.
    monkeypatch.setenv("MOKO_HOME", str(tmp_path))
    return ProcessSupervisor()


async def _wait_status(sup, name, status, tries=50):
    for _ in range(tries):
        row = next((p for p in sup.list() if p["name"] == name), None)
        if row and row["status"] == status:
            return row
        await asyncio.sleep(0.05)
    return next((p for p in sup.list() if p["name"] == name), None)


async def test_start_captures_output_and_exit(sup):
    msg = await sup.start("greeter", "echo hello-from-moko")
    assert "greeter" in msg
    row = await _wait_status(sup, "greeter", "exited")
    assert row is not None and row["status"] == "exited" and row["exit_code"] == 0
    assert "hello-from-moko" in sup.logs("greeter")


async def test_list_and_stop_a_long_runner(sup):
    await sup.start("sleeper", "sleep 30")
    row = next(p for p in sup.list() if p["name"] == "sleeper")
    assert row["status"] == "running" and row["pid"] > 0

    out = await sup.stop("sleeper")
    assert "Stopped" in out
    assert next(p for p in sup.list() if p["name"] == "sleeper")["status"] == "stopped"


async def test_duplicate_name_while_running_is_refused(sup):
    await sup.start("dup", "sleep 30")
    again = await sup.start("dup", "sleep 30")
    assert "already running" in again
    await sup.stop("dup")


async def test_stop_unknown_is_graceful(sup):
    assert "No process named" in await sup.stop("ghost")
    assert "No process named" in sup.logs("ghost")


async def test_registry_persists_across_instances(sup, monkeypatch, tmp_path):
    await sup.start("persisted", "sleep 30")
    # A fresh supervisor (as if Moko restarted) reads the registry and still
    # knows about it; while the PID is alive it's reported running.
    fresh = ProcessSupervisor()
    names = {p["name"] for p in fresh.list()}
    assert "persisted" in names
    await sup.stop("persisted")
