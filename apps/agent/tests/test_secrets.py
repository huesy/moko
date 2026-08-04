import os
import stat

from moko.config import secrets


def test_get_missing_key_is_none(tmp_path, monkeypatch):
    monkeypatch.setenv("MOKO_HOME", str(tmp_path))
    assert secrets.get_api_key("anthropic") is None


def test_set_then_get_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setenv("MOKO_HOME", str(tmp_path))
    secrets.set_api_key("anthropic", "sk-test-123")
    assert secrets.get_api_key("anthropic") == "sk-test-123"
    # A second provider doesn't clobber the first.
    secrets.set_api_key("openai", "sk-openai-456")
    assert secrets.get_api_key("anthropic") == "sk-test-123"
    assert secrets.get_api_key("openai") == "sk-openai-456"


def test_secrets_file_is_owner_only(tmp_path, monkeypatch):
    monkeypatch.setenv("MOKO_HOME", str(tmp_path))
    secrets.set_api_key("anthropic", "sk-test")
    mode = stat.S_IMODE(os.stat(secrets.secrets_path()).st_mode)
    assert mode == 0o600


def test_secrets_are_owner_only_under_a_tight_umask(tmp_path, monkeypatch):
    # The bug: open('w') created the file at the umask (0644) with live tokens
    # before chmod narrowed it — a world-readable window, and 0644 forever if the
    # process died in between. os.open(...0600) makes it owner-only from byte zero,
    # regardless of umask. Force a permissive umask to prove the file is still 0600.
    monkeypatch.setenv("MOKO_HOME", str(tmp_path))
    old = os.umask(0o000)
    try:
        secrets.set_secret("telegram_token", "123:abc")
    finally:
        os.umask(old)
    mode = stat.S_IMODE(os.stat(secrets.secrets_path()).st_mode)
    assert mode == 0o600
    # no leftover temp file from the atomic write
    assert not (secrets.secrets_path().with_name("secrets.yaml.tmp")).exists()


def test_home_dir_is_owner_only(tmp_path, monkeypatch):
    from moko.config.paths import ensure_home

    monkeypatch.setenv("MOKO_HOME", str(tmp_path / "h"))
    home = ensure_home()
    assert stat.S_IMODE(os.stat(home).st_mode) == 0o700  # the private mind isn't world-readable
