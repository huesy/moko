"""The non-interactive `moko config KEY [VALUE]` get/set/list surface."""

import pytest

from moko.cli import config as cli_config
from moko.config.config import Config


@pytest.fixture
def config(monkeypatch):
    c = Config(
        companion_name="Andrew",
        timezone="Australia/Brisbane",
        sleep_start="23:00",
        sleep_end="07:00",
        proactivity="moderate",
        interests=["calendar"],
        llm_provider="anthropic",
        llm_model="claude-opus-4-8",
    )
    saved: list[bool] = []
    monkeypatch.setattr(c, "save", lambda: saved.append(True))
    c._saved = saved  # type: ignore[attr-defined]
    monkeypatch.setattr(cli_config.cfg, "load", lambda: c)
    # Keep the post-set restart hint from shelling out to systemctl in tests.
    monkeypatch.setattr(cli_config, "_apply_hint", lambda **_: None)
    return c


def test_get_prints_the_bare_value(config, capsys):
    assert cli_config.run(["proactivity"]) == 0
    assert capsys.readouterr().out.strip() == "moderate"


def test_set_validates_and_saves(config):
    assert cli_config.run(["proactivity", "chatty"]) == 0
    assert config.proactivity == "chatty"
    assert config._saved == [True]


def test_set_unquoted_multiword_name(config):
    assert cli_config.run(["companion.name", "Andrew", "Hughes"]) == 0
    assert config.companion_name == "Andrew Hughes"


def test_set_rejects_bad_enum_without_saving(config):
    assert cli_config.run(["proactivity", "loud"]) == 1
    assert config.proactivity == "moderate"  # unchanged
    assert config._saved == []  # not written


def test_set_rejects_bad_timezone_and_bad_int(config):
    assert cli_config.run(["timezone", "Mars/Olympus"]) == 1
    assert cli_config.run(["cadence.outreach_settle_minutes", "soon"]) == 1
    assert cli_config.run(["cadence.outreach_settle_minutes", "-5"]) == 1
    assert config._saved == []


def test_cadence_set_coerces_to_int(config):
    assert cli_config.run(["cadence.reflect_muse_interval_minutes", "45"]) == 0
    assert config.cadence.reflect_muse_interval_minutes == 45


def test_interests_csv_roundtrip(config, capsys):
    assert cli_config.run(["interests", "calendar,curious"]) == 0
    assert config.interests == ["calendar", "curious"]
    capsys.readouterr()
    cli_config.run(["interests"])
    assert capsys.readouterr().out.strip() == "calendar,curious"


def test_interests_rejects_unknown(config):
    assert cli_config.run(["interests", "calendar,flying"]) == 1
    assert config.interests == ["calendar"]


def test_brain_and_access_are_read_only_here(config):
    # Readable…
    assert cli_config.run(["llm.provider"]) == 0
    # …but not settable via the flat CLI (would half-configure a coupled cluster).
    assert cli_config.run(["llm.provider", "openai"]) == 1
    assert config.llm_provider == "anthropic"
    assert cli_config.run(["access.monitor", "network"]) == 1
    assert config.monitor_access == "local"


def test_unknown_key_is_an_error(config):
    assert cli_config.run(["nope.nope"]) == 1
    assert cli_config.run(["nope.nope", "x"]) == 1


def test_list_shows_every_key(config, capsys):
    assert cli_config.run([], list_all=True) == 0
    out = capsys.readouterr().out
    assert "proactivity" in out and "moderate" in out
    assert "llm.provider" in out and "read-only" in out


# ── runtime tier (SANDBOX.md) ────────────────────────────────────────────────
def test_set_runtime_mode(config):
    assert cli_config.run(["runtime.mode", "podman"]) == 0
    assert config.runtime.mode == "podman"
    assert config._saved == [True]


def test_set_runtime_mode_rejects_bad_without_saving(config):
    assert cli_config.run(["runtime.mode", "vm"]) == 1
    assert config.runtime.mode == "host"
    assert config._saved == []


def test_set_runtime_hardened(config):
    assert cli_config.run(["runtime.hardened", "strict"]) == 0
    assert config.runtime.hardened == "strict"


def test_set_runtime_mounts_splits_a_list(config):
    assert cli_config.run(["runtime.mounts", "/home/x/proj, /data"]) == 0
    assert config.runtime.mounts == ["/home/x/proj", "/data"]


def test_set_runtime_port_validates_range(config):
    assert cli_config.run(["runtime.monitor_port", "70000"]) == 1
    assert config._saved == []
    assert cli_config.run(["runtime.monitor_port", "4500"]) == 0
    assert config.runtime.monitor_port == 4500


def test_get_runtime_mode_defaults_to_host(config, capsys):
    assert cli_config.run(["runtime.mode"]) == 0
    assert capsys.readouterr().out.strip() == "host"
