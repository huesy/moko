"""Exposure tiers + the monitor's auth gate."""

import pytest
from aiohttp.test_utils import TestClient, TestServer

from moko.config.config import Config
from moko.monitor import access
from moko.monitor.server import build_app
from moko.store import Store

CFG = Config(
    companion_name="Jordan", timezone="UTC", sleep_start="23:00",
    sleep_end="07:00", proactivity="moderate",
)


@pytest.fixture
async def store(tmp_path):
    s = await Store.open(tmp_path / "moko.db")
    try:
        yield s
    finally:
        await s.close()


def test_local_needs_no_auth():
    assert access.requires_auth("local") is False
    host, problem = access.resolve_host("local")
    assert host == "127.0.0.1"
    assert problem is None


def test_network_binds_all_interfaces_and_needs_auth():
    assert access.requires_auth("network") is True
    host, problem = access.resolve_host("network")
    assert host == "0.0.0.0"
    assert problem is None


def test_tailscale_without_tailscale_falls_back_local(monkeypatch):
    monkeypatch.setattr(access, "tailscale_ip", lambda: None)
    host, problem = access.resolve_host("tailscale")
    assert host == "127.0.0.1"
    assert problem  # a human-readable explanation


def test_cgnat_detection():
    assert access._is_cgnat("100.84.220.11") is True
    assert access._is_cgnat("192.168.0.5") is False
    assert access._is_cgnat("not-an-ip") is False


def test_config_round_trips_monitor_access():
    cfg = Config(
        companion_name="A", timezone="UTC", sleep_start="23:00",
        sleep_end="07:00", proactivity="gentle", monitor_access="tailscale",
    )
    assert Config.from_dict(cfg.to_dict()).monitor_access == "tailscale"


def test_old_config_defaults_to_local():
    # A config written before this feature has no "access" key.
    assert Config.from_dict({"companion": {"name": "A"}}).monitor_access == "local"


# ---- the auth gate ----

async def test_no_token_means_open(store):
    client = TestClient(TestServer(build_app(store, CFG)))  # local tier: no token
    await client.start_server()
    try:
        assert (await client.get("/api/status")).status == 200
    finally:
        await client.close()


async def test_token_blocks_unauthenticated_api(store):
    client = TestClient(TestServer(build_app(store, CFG, token="s3cret")))
    await client.start_server()
    try:
        resp = await client.get("/api/status")
        assert resp.status == 401
    finally:
        await client.close()


async def test_page_load_unauthenticated_shows_login(store):
    client = TestClient(TestServer(build_app(store, CFG, token="s3cret")))
    await client.start_server()
    try:
        resp = await client.get("/")
        assert resp.status == 401
        assert "Access password" in await resp.text()
    finally:
        await client.close()


async def test_login_sets_cookie_and_unlocks(store):
    client = TestClient(TestServer(build_app(store, CFG, token="s3cret")))
    await client.start_server()
    try:
        # Wrong password is rejected.
        bad = await client.post("/login", data={"token": "nope"}, allow_redirects=False)
        assert bad.status == 401
        # Correct password sets the cookie (the client jar keeps it)...
        ok = await client.post("/login", data={"token": "s3cret"}, allow_redirects=False)
        assert ok.status == 302
        # ...and now the API is reachable.
        assert (await client.get("/api/status")).status == 200
    finally:
        await client.close()
