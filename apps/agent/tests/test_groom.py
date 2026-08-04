"""`moko groom` — outward maintenance checks (token freshness, claude CLI, etc.)."""

from moko.cli import groom
from moko.clock import now_utc
from moko.config import config as cfg


def _config(**kw):
    base = dict(
        companion_name="A", timezone="UTC", sleep_start="23:00", sleep_end="07:00",
        proactivity="moderate",
    )
    base.update(kw)
    return cfg.Config(**base)


def test_token_check_skipped_when_not_claude_code():
    name, status, _ = groom._token_check(_config(llm_auth_method="api_key"))
    assert name == "Subscription" and status == groom._INFO


def test_token_check_reports_remaining_validity(monkeypatch):
    from moko.llm import claude_oauth

    future_ms = (now_utc().timestamp() + 5 * 3600) * 1000  # ~5h left
    monkeypatch.setattr(
        claude_oauth, "read_credentials", lambda: {"accessToken": "x", "expiresAt": future_ms}
    )
    _, status, detail = groom._token_check(_config(llm_auth_method="claude_code"))
    assert status == groom._OK and "valid" in detail


def test_token_check_refreshes_when_near_expiry(monkeypatch):
    from moko.llm import claude_oauth

    soon_ms = (now_utc().timestamp() + 600) * 1000  # ~10 min left → below threshold
    monkeypatch.setattr(
        claude_oauth, "read_credentials",
        lambda: {"accessToken": "x", "refreshToken": "r", "expiresAt": soon_ms},
    )
    refreshed = []
    monkeypatch.setattr(claude_oauth, "refresh", lambda rt: refreshed.append(rt))
    _, status, detail = groom._token_check(_config(llm_auth_method="claude_code"))
    assert refreshed == ["r"] and status == groom._OK and "refreshed" in detail


def test_token_check_warns_when_not_signed_in(monkeypatch):
    from moko.llm import claude_oauth

    monkeypatch.setattr(claude_oauth, "read_credentials", lambda: None)
    _, status, _ = groom._token_check(_config(llm_auth_method="claude_code"))
    assert status == groom._WARN
