"""Claude subscription auth via the Claude Code OAuth token.

Moko bills against the companion's Claude plan by reading the OAuth token that
`claude` (Claude Code) stores after login, and calling the **standard Anthropic
Messages API** with it: Bearer auth plus the Claude Code identity headers and
system-prompt prefix the subscription path requires. No agent SDK, no subprocess
per turn — just our normal `AnthropicClient` with a different credential. Tokens
are refreshed in place against the OAuth endpoint when they near expiry.

This presents Claude Code's identity to the API (the documented subscription
transport), so the `user-agent` version must stay reasonably current.
"""

from __future__ import annotations

import json
import subprocess
import urllib.error
import urllib.request
from pathlib import Path

from ..clock import now_utc

# Where `claude` stores the login on Linux/macOS (JSON file form).
_CREDS_PATH = Path.home() / ".claude" / ".credentials.json"
# Claude Code's public OAuth client + token endpoints (refresh grant).
_CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
_TOKEN_ENDPOINTS = (
    "https://platform.claude.com/v1/oauth/token",
    "https://console.anthropic.com/v1/oauth/token",
)
# The identity the subscription path validates on. The system prefix and these
# betas are required for OAuth traffic to be accepted/routed.
_BETAS = "claude-code-20250219,oauth-2025-04-20"
SYSTEM_PREFIX = "You are Claude Code, Anthropic's official CLI for Claude."
_VERSION_FALLBACK = "2.1.74"
_version_cache: str | None = None


def read_credentials() -> dict | None:
    """The stored OAuth blob (accessToken / refreshToken / expiresAt), or None
    if `claude` hasn't been signed in on this machine."""
    try:
        data = json.loads(_CREDS_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    oauth = data.get("claudeAiOauth")
    return oauth if isinstance(oauth, dict) and oauth.get("accessToken") else None


def claude_code_version() -> str:
    """The installed Claude Code version — its user-agent must be ~current or
    the API rejects OAuth requests. Detected once, with a recent fallback."""
    global _version_cache
    if _version_cache is not None:
        return _version_cache
    for cmd in ("claude", "claude-code"):
        try:
            r = subprocess.run([cmd, "--version"], capture_output=True, text=True, timeout=5)
        except (OSError, subprocess.SubprocessError):
            continue
        out = (r.stdout or "").strip().split()
        if r.returncode == 0 and out and out[0][:1].isdigit():
            _version_cache = out[0]
            return _version_cache
    _version_cache = _VERSION_FALLBACK
    return _version_cache


def headers() -> dict[str, str]:
    """The default headers every subscription request needs."""
    return {
        "anthropic-beta": _BETAS,
        "user-agent": f"claude-cli/{claude_code_version()} (external, cli)",
        "x-app": "cli",
    }


def refresh(refresh_token: str) -> dict:
    """Exchange a refresh token for a fresh access token and write it back to
    the shared credentials file — the single source of truth, so Moko and
    `claude` don't invalidate each other's (single-use) refresh token. Returns
    the updated `claudeAiOauth` blob."""
    if not refresh_token:
        raise ValueError("no refresh token")
    body = json.dumps(
        {"grant_type": "refresh_token", "refresh_token": refresh_token, "client_id": _CLIENT_ID}
    ).encode()
    last: Exception | None = None
    for endpoint in _TOKEN_ENDPOINTS:
        req = urllib.request.Request(
            endpoint,
            data=body,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "User-Agent": f"claude-cli/{claude_code_version()} (external, cli)",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310 — fixed https hosts
                return _persist(json.loads(resp.read()))
        except (urllib.error.URLError, ValueError, OSError) as exc:
            last = exc
    raise RuntimeError(f"Claude token refresh failed: {last}")


def _persist(tok: dict) -> dict:
    """Merge refreshed tokens into ~/.claude/.credentials.json (preserving the
    other fields) and return the updated blob."""
    try:
        data = json.loads(_CREDS_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    oauth = data.setdefault("claudeAiOauth", {})
    oauth["accessToken"] = tok["access_token"]
    if tok.get("refresh_token"):
        oauth["refreshToken"] = tok["refresh_token"]
    if tok.get("expires_in"):
        oauth["expiresAt"] = int((now_utc().timestamp() + tok["expires_in"]) * 1000)
    _CREDS_PATH.write_text(json.dumps(data), encoding="utf-8")
    return oauth
