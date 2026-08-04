"""Secrets — API keys and the like.

These never go in the repo and never in config.yaml. They live in
~/.moko/secrets.yaml with owner-only (0600) permissions.
"""

from __future__ import annotations

import os
import stat

import yaml

from .paths import ensure_home, moko_home


def secrets_path():
    return moko_home() / "secrets.yaml"


def _load() -> dict:
    path = secrets_path()
    if not path.exists():
        return {}
    with path.open(encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def _save(data: dict) -> None:
    ensure_home()
    path = secrets_path()
    text = yaml.safe_dump(data, sort_keys=False, allow_unicode=True)
    # Write restricted from byte zero (0600 via os.open) and replace atomically.
    # The old "open('w') then chmod" left the file at the umask (0644) with live
    # tokens for a window — world-readable on a shared box, and permanently 0644 if
    # the process died in between. A truncating in-place write also risked a
    # corrupt/empty secrets file on a crash; os.replace makes it all-or-nothing.
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, stat.S_IRUSR | stat.S_IWUSR)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)  # atomic; the replacement carries tmp's 0600 perms
    finally:
        if tmp.exists():
            tmp.unlink()
    os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)  # belt-and-braces: 0600 even if pre-existing


def set_api_key(provider: str, key: str) -> None:
    data = _load()
    data.setdefault("api_keys", {})[provider] = key
    _save(data)


def get_api_key(provider: str) -> str | None:
    return (_load().get("api_keys") or {}).get(provider)


def set_secret(name: str, value: str) -> None:
    """Store a named secret (e.g. a Telegram bot token)."""
    data = _load()
    data.setdefault("secrets", {})[name] = value
    _save(data)


def get_secret(name: str) -> str | None:
    return (_load().get("secrets") or {}).get(name)


def all_secrets() -> dict[str, str]:
    """Every named secret (name → value). Used to inject referenced credentials
    into a command's environment and to redact values from captured output — so
    skills can *use* a secret without Moko (the LLM) ever seeing it."""
    return {str(k): str(v) for k, v in (_load().get("secrets") or {}).items()}


def secret_names() -> list[str]:
    """The names of stored secrets (never the values) — safe to show the model."""
    return sorted((_load().get("secrets") or {}).keys())


def remove_secret(name: str) -> bool:
    data = _load()
    secrets = data.get("secrets") or {}
    if name not in secrets:
        return False
    del secrets[name]
    _save(data)
    return True


def set_oauth_tokens(provider: str, tokens: dict) -> None:
    """Store an OAuth token bundle (access/refresh/expiry + endpoint/client)."""
    data = _load()
    data.setdefault("oauth", {})[provider] = tokens
    _save(data)


def get_oauth_tokens(provider: str) -> dict | None:
    return (_load().get("oauth") or {}).get(provider)
