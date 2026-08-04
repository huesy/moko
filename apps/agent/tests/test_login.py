"""Provider-aware login: auth status is per-provider (not per-role), so one Grok
subscription can power the brain *and* image/video/voice. These cover the pure
helpers `moko login` and inline setup/config sign-in are built from — the device
flow itself (browser approval) isn't exercised."""

from moko.cli import login
from moko.config import secrets
from moko.config.config import Config, ModelChoice


def _cfg(**kw) -> Config:
    base = dict(companion_name="A", timezone="UTC", sleep_start="23:00",
                sleep_end="07:00", proactivity="moderate")
    base.update(kw)
    return Config(**base)


def test_xai_status_tracks_the_stored_subscription(tmp_path, monkeypatch):
    monkeypatch.setenv("MOKO_HOME", str(tmp_path))
    assert login.xai_status() == "missing"  # nothing connected
    secrets.set_oauth_tokens("xai", {"access_token": "t", "refresh_token": "r"})
    assert login.xai_status() == "connected"  # a refresh token self-heals → connected
    secrets.set_oauth_tokens(
        "xai", {"access_token": "t", "expires_at": "2000-01-01T00:00:00+00:00"})
    assert login.xai_status() == "expired"  # past expiry, no refresh token


def test_ensure_xai_connected_is_a_noop_when_already_connected(tmp_path, monkeypatch):
    monkeypatch.setenv("MOKO_HOME", str(tmp_path))
    secrets.set_oauth_tokens("xai", {"access_token": "t", "refresh_token": "r"})
    # Already connected → returns True without touching the device flow.
    assert login.ensure_xai_connected(interactive=False) is True


def test_ensure_xai_connected_non_interactive_wont_start_a_flow(tmp_path, monkeypatch):
    monkeypatch.setenv("MOKO_HOME", str(tmp_path))
    assert login.ensure_xai_connected(interactive=False) is False  # not connected, can't prompt


def test_slots_cover_brain_and_every_configured_modality():
    c = _cfg(llm_provider="anthropic", llm_auth_method="claude_code")
    c.models["image"] = ModelChoice(provider="xai", model="grok-imagine-image", auth="oauth")
    c.models["speech"] = ModelChoice(provider="openai", model="gpt-4o-mini-tts", auth="api_key")
    roles = {role: (prov, auth) for role, prov, auth in login._slots(c)}
    assert roles["brain"] == ("anthropic", "claude_code")
    assert roles["image"] == ("xai", "oauth")
    assert roles["speech"] == ("openai", "api_key")
    assert "vision" not in roles  # unconfigured senses don't appear


def test_provider_status_reads_each_auth_kind(tmp_path, monkeypatch):
    monkeypatch.setenv("MOKO_HOME", str(tmp_path))
    # xAI via subscription: reflects the OAuth status.
    assert login._provider_status("xai", "oauth") == "not connected"
    secrets.set_oauth_tokens("xai", {"access_token": "t", "refresh_token": "r"})
    assert login._provider_status("xai", "oauth") == "connected"
    # API-key provider: reflects whether a key is stored.
    assert login._provider_status("openai", "api_key") == "no key"
    secrets.set_api_key("openai", "k")
    assert login._provider_status("openai", "api_key") == "key set"


def test_provider_status_anthropic_uses_machine_claude_code(tmp_path, monkeypatch):
    monkeypatch.setenv("MOKO_HOME", str(tmp_path))
    from moko.llm import claude_oauth

    # Anthropic's status reads this machine's Claude Code sign-in (never "no key" —
    # there's no stored key to check), so it reflects whether those creds are present.
    monkeypatch.setattr(claude_oauth, "read_credentials", lambda: None)
    assert login._provider_status("anthropic", "claude_code") == "not signed in"
    monkeypatch.setattr(claude_oauth, "read_credentials", lambda: {"access_token": "x"})
    assert login._provider_status("anthropic", "claude_code") == "connected (machine)"
