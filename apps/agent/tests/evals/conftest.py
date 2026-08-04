"""Behavioral evals — real Brain turns against the configured provider.

The unit suite fakes the LLM (see tests/test_brain.py's `FakeLLM`), so a whole
class of *behavioral* regressions — the ones that live in how the real model
reads affect, resists coercion, or double-records — is invisible to it. These
evals close that gap: each scenario drives a REAL `Brain.respond` turn and
asserts *structurally* on the resulting store state (recorded valence, the
commitment count, the tool-call sequence), never on wording.

Two things keep this safe to sit in the ordinary suite:

  * **It auto-skips unless a developer opts in AND a provider resolves.** These
    fire real, billable LLM calls, so a stray ~/.moko on a dev box must NOT make an
    ordinary `pytest tests/evals` run spend money — that would break the repo's
    "don't blast APIs" rule. So `live_llm` requires the `MOKO_EVAL_LLM` env flag to
    be set *and* a usable provider (real config + secrets, built via the production
    `build_client`). Missing either → the whole module skips. `uv run pytest
    tests/evals` is therefore green — as skips — in CI and on a dev machine alike,
    with zero network calls, until you ask for them.
  * **It never touches the real mind.** The Brain gets a throwaway `Store` on a
    temp DB (the `eval_store` fixture); only the LLM is real. The provocations
    land in a fresh graph that's discarded when the test ends.

The `live` marker tags every eval test so a run can select or deselect them
(`-m live` / `-m "not live"`). It's registered here, locally, so the root pytest
config stays untouched.
"""

from __future__ import annotations

import contextlib
import os
from pathlib import Path

import pytest

from moko.config import config as config_mod
from moko.llm import UnsupportedAuth, build_client
from moko.store import Store

# The opt-in gate. These evals make real, billable provider calls, so they only run
# when a developer explicitly asks — merely having a configured ~/.moko on the box is
# NOT enough (an ordinary test run must never quietly spend money or hit the network).
_OPT_IN_ENV = "MOKO_EVAL_LLM"


def pytest_configure(config: pytest.Config) -> None:
    """Register the `live` marker locally, so tagging an eval doesn't warn and the
    root pyproject.toml never has to learn about it."""
    config.addinivalue_line(
        "markers",
        "live: a behavioral eval that drives a real LLM turn (skipped without a provider)",
    )


@contextlib.contextmanager
def _real_moko_home():
    """Point path resolution at the companion's TRUE ~/.moko for the duration of the
    block, then restore. The root conftest sets `MOKO_HOME` to a throwaway temp dir so
    unit tests can't read or write the live install — but detecting whether a *real*
    provider is configured means reading the real config.yaml + secrets.yaml. We borrow
    the real home only to load config and build the client; the Brain's store stays
    isolated (see `eval_store`)."""
    saved = os.environ.get("MOKO_HOME")
    # A test may point MOKO_HOME at ~/.moko explicitly; otherwise fall back to the
    # default home. Either way we want the *real* install, not the per-test temp dir.
    if saved and Path(saved).expanduser() == (Path.home() / ".moko"):
        yield  # already the real home
        return
    os.environ.pop("MOKO_HOME", None)  # → moko_home() resolves to ~/.moko
    try:
        yield
    finally:
        if saved is not None:
            os.environ["MOKO_HOME"] = saved
        else:
            os.environ.pop("MOKO_HOME", None)


@pytest.fixture(scope="session")
def live_llm():
    """A real, configured `LLMClient`, or skip the whole module.

    Requires the `MOKO_EVAL_LLM` opt-in flag first (so an ordinary run never spends),
    then resolves the companion's actual config + secrets and builds a client through
    the production path (`build_client`). Any of "not opted in", "not set up", "no
    provider chosen", or "credentials missing" → skip, so CI stays green and offline."""
    if not os.environ.get(_OPT_IN_ENV):
        pytest.skip(f"behavioral evals are opt-in — set {_OPT_IN_ENV}=1 to run them")
    with _real_moko_home():
        cfg = config_mod.load()
        if cfg is None:
            pytest.skip("no ~/.moko config — behavioral evals need a configured provider")
        if not cfg.llm_provider or cfg.llm_provider == "none":
            pytest.skip("no LLM provider configured — behavioral evals need one")
        try:
            client = build_client(cfg)
        except UnsupportedAuth as exc:
            pytest.skip(f"LLM provider not usable ({exc}) — behavioral evals need credentials")
    if client is None:
        pytest.skip("no LLM client could be built — behavioral evals need a provider")
    return client


@pytest.fixture(scope="session")
def live_config():
    """The companion's real config, alongside `live_llm`. Session-scoped so config +
    secrets are read once. Skips on the same conditions as `live_llm`."""
    if not os.environ.get(_OPT_IN_ENV):
        pytest.skip(f"behavioral evals are opt-in — set {_OPT_IN_ENV}=1 to run them")
    with _real_moko_home():
        cfg = config_mod.load()
    if cfg is None or not cfg.llm_provider or cfg.llm_provider == "none":
        pytest.skip("no LLM provider configured — behavioral evals need one")
    return cfg


@pytest.fixture
async def eval_store(tmp_path):
    """A throwaway `Store` for one eval — a fresh temp DB the provocations land in,
    never the companion's real mind. Matches the unit suite's `store` fixture."""
    store = await Store.open(tmp_path / "eval.db")
    try:
        yield store
    finally:
        await store.close()
