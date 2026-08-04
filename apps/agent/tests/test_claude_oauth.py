"""Claude subscription via OAuth: credential read, identity headers, the system
prefix, and the AnthropicClient Bearer/refresh path."""

import json

from moko.llm import claude_oauth
from moko.llm.anthropic_client import AnthropicClient, _system_param
from moko.llm.base import Msg, System


def test_read_credentials_parses_the_blob(tmp_path, monkeypatch):
    creds = tmp_path / "creds.json"
    creds.write_text(json.dumps({"claudeAiOauth": {"accessToken": "sk-ant-oat-x", "expiresAt": 1}}))
    monkeypatch.setattr(claude_oauth, "_CREDS_PATH", creds)
    blob = claude_oauth.read_credentials()
    assert blob and blob["accessToken"] == "sk-ant-oat-x"


def test_read_credentials_none_when_missing_or_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(claude_oauth, "_CREDS_PATH", tmp_path / "nope.json")
    assert claude_oauth.read_credentials() is None
    empty = tmp_path / "empty.json"
    empty.write_text(json.dumps({"claudeAiOauth": {}}))  # no accessToken
    monkeypatch.setattr(claude_oauth, "_CREDS_PATH", empty)
    assert claude_oauth.read_credentials() is None


def test_headers_carry_the_claude_code_identity(monkeypatch):
    monkeypatch.setattr(claude_oauth, "claude_code_version", lambda: "9.9.9")
    h = claude_oauth.headers()
    assert "oauth-2025-04-20" in h["anthropic-beta"]
    assert h["user-agent"] == "claude-cli/9.9.9 (external, cli)"
    assert h["x-app"] == "cli"


def test_system_prefix_only_in_oauth_mode():
    sys_obj = System(stable="core", volatile="clock")
    oauth = _system_param(sys_obj, oauth=True)
    plain = _system_param(sys_obj, oauth=False)
    assert oauth[0]["text"] == claude_oauth.SYSTEM_PREFIX  # CC identity prepended
    assert plain[0]["text"] == "core"  # no prefix on the API-key path
    # the cache breakpoint still sits on the stable block in both
    assert any(b.get("cache_control") for b in oauth)


# ── AnthropicClient Bearer + refresh ───────────────────────────────────
class _FakeMessages:
    def __init__(self):
        self.calls = []

    async def create(self, **kw):
        self.calls.append(kw)
        block = type("B", (), {"type": "text", "text": "ok"})()
        usage = type("U", (), {"input_tokens": 1, "output_tokens": 1})()
        return type("R", (), {"content": [block], "usage": usage})()


class _FakeAnthropic:
    built = []

    def __init__(self, **kw):
        self.kw = kw
        self.messages = _FakeMessages()
        _FakeAnthropic.built.append(kw)


async def test_oauth_client_uses_bearer_and_refreshes_when_expired(monkeypatch):
    _FakeAnthropic.built = []
    monkeypatch.setattr("moko.llm.anthropic_client.AsyncAnthropic", _FakeAnthropic)
    monkeypatch.setattr(claude_oauth, "read_credentials", lambda: None)  # nothing fresher on disk
    refreshed = {"accessToken": "sk-ant-oat-NEW", "refreshToken": "r2", "expiresAt": 9e12}
    monkeypatch.setattr(claude_oauth, "refresh", lambda rt: refreshed)

    client = AnthropicClient(
        "claude-opus-4-8",
        oauth={"access_token": "sk-ant-oat-OLD", "refresh_token": "r1", "expires_at_ms": 1},
    )
    assert _FakeAnthropic.built[0]["auth_token"] == "sk-ant-oat-OLD"  # Bearer, not x-api-key
    assert "api_key" not in _FakeAnthropic.built[0]

    await client.complete("sys", [Msg("user", "hi")])  # token is expired → proactive refresh
    assert _FakeAnthropic.built[-1]["auth_token"] == "sk-ant-oat-NEW"  # rebuilt with fresh token


async def test_oauth_adopts_a_freshly_rotated_disk_token(monkeypatch):
    # The `claude` CLI just refreshed: the shared file holds a still-valid token.
    # Moko adopts it instead of spending its own (single-use) refresh — no race.
    _FakeAnthropic.built = []
    monkeypatch.setattr("moko.llm.anthropic_client.AsyncAnthropic", _FakeAnthropic)
    monkeypatch.setattr(
        claude_oauth, "read_credentials",
        lambda: {"accessToken": "sk-ant-oat-DISK", "refreshToken": "rD", "expiresAt": 9e12},
    )

    def _boom(rt):
        raise AssertionError("must not refresh when the disk token is already valid")

    monkeypatch.setattr(claude_oauth, "refresh", _boom)
    client = AnthropicClient(
        "claude-opus-4-8",
        oauth={"access_token": "sk-ant-oat-OLD", "refresh_token": "r1", "expires_at_ms": 1},
    )
    await client.complete("sys", [Msg("user", "hi")])  # expired in-memory → re-read → adopt
    assert _FakeAnthropic.built[-1]["auth_token"] == "sk-ant-oat-DISK"


async def test_oauth_refreshes_with_the_disk_token_not_the_stale_one(monkeypatch):
    # Disk's access token is also expired, but the CLI rotated its refresh token.
    # Moko must exchange the file's LATEST refresh token, not its stale in-memory
    # one, or it gets invalid_grant and silently stops answering until restart.
    _FakeAnthropic.built = []
    monkeypatch.setattr("moko.llm.anthropic_client.AsyncAnthropic", _FakeAnthropic)
    monkeypatch.setattr(
        claude_oauth, "read_credentials",
        lambda: {"accessToken": "sk-ant-oat-DISKOLD", "refreshToken": "rDISK", "expiresAt": 1},
    )
    used = {}

    def _refresh(rt):
        used["token"] = rt
        return {"accessToken": "sk-ant-oat-NEW", "refreshToken": "rNEW", "expiresAt": 9e12}

    monkeypatch.setattr(claude_oauth, "refresh", _refresh)
    client = AnthropicClient(
        "claude-opus-4-8",
        oauth={"access_token": "sk-ant-oat-OLD", "refresh_token": "rSTALE", "expires_at_ms": 1},
    )
    await client.complete("sys", [Msg("user", "hi")])
    assert used["token"] == "rDISK"  # the file's latest, not the stale "rSTALE"
    assert _FakeAnthropic.built[-1]["auth_token"] == "sk-ant-oat-NEW"


async def test_api_key_client_uses_x_api_key(monkeypatch):
    _FakeAnthropic.built = []
    monkeypatch.setattr("moko.llm.anthropic_client.AsyncAnthropic", _FakeAnthropic)
    AnthropicClient("claude-opus-4-8", api_key="sk-ant-api-123")
    assert _FakeAnthropic.built[0]["api_key"] == "sk-ant-api-123"
    assert "auth_token" not in _FakeAnthropic.built[0]
