"""Test isolation: every test runs against a throwaway MOKO_HOME, never the
companion's real ~/.moko. Without this, anything that touches the policy store,
secrets, or the work folder (the shell classifier creates it) would read and
*write* the live install — a real side effect, and cross-test leakage."""

import os
import tempfile

import pytest

# Set at import — before any moko module resolves a path — so even import-time
# path lookups land in a temp dir, not ~/.moko.
os.environ.setdefault("MOKO_HOME", tempfile.mkdtemp(prefix="moko-tests-"))


@pytest.fixture(autouse=True)
def _isolated_moko_home(tmp_path, monkeypatch):
    """A fresh MOKO_HOME per test, so policy/secret/workspace writes can't leak
    between tests (or onto the real machine). `moko_home()` reads the env each
    call, so pointing it here takes effect immediately."""
    monkeypatch.setenv("MOKO_HOME", str(tmp_path / "moko_home"))
