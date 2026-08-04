from moko.config import config as cfg


def test_not_set_up_initially(tmp_path, monkeypatch):
    monkeypatch.setenv("MOKO_HOME", str(tmp_path))
    assert cfg.exists() is False
    assert cfg.load() is None


def test_save_then_load_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setenv("MOKO_HOME", str(tmp_path))
    config = cfg.Config(
        companion_name="Jordan",
        timezone="Asia/Tokyo",
        sleep_start="23:00",
        sleep_end="07:00",
        proactivity="moderate",
        interests=["calendar", "curious"],
        llm_provider="anthropic",
        llm_model="claude-opus-4-8",
        llm_auth_method="claude_code",
        created_at="2026-06-22T00:00:00+00:00",
    )
    path = config.save()
    assert path.exists()
    assert cfg.exists() is True

    loaded = cfg.load()
    assert loaded is not None
    assert loaded.companion_name == "Jordan"
    assert loaded.timezone == "Asia/Tokyo"
    assert loaded.sleep_start == "23:00"
    assert loaded.sleep_end == "07:00"
    assert loaded.proactivity == "moderate"
    assert loaded.interests == ["calendar", "curious"]
    assert loaded.llm_provider == "anthropic"
    assert loaded.llm_model == "claude-opus-4-8"
    assert loaded.llm_auth_method == "claude_code"
    assert loaded.version == cfg.CONFIG_VERSION


def test_from_dict_tolerates_missing_fields():
    loaded = cfg.Config.from_dict({})
    assert loaded.timezone == "UTC"
    assert loaded.proactivity == "moderate"
    assert loaded.interests == []
    assert loaded.llm_provider == "none"
    assert loaded.llm_model == ""
    assert loaded.chat_verbosity == "normal"  # default when unset
    # An older config with no cadence block falls back to the defaults.
    assert loaded.cadence.outreach_settle_minutes == 30
    assert loaded.cadence.reflect_muse_interval_minutes == 30
    # Autonomous research is OFF for any config that never opted in (secure default;
    # an existing install stops autonomous research on upgrade until turned on).
    assert loaded.allow_autonomous_research is False


def test_autonomous_research_roundtrips(tmp_path, monkeypatch):
    monkeypatch.setenv("MOKO_HOME", str(tmp_path))
    config = cfg.Config(
        companion_name="Jordan",
        timezone="Asia/Tokyo",
        sleep_start="23:00",
        sleep_end="07:00",
        proactivity="moderate",
        allow_autonomous_research=True,
    )
    config.save()
    assert cfg.load().allow_autonomous_research is True


def test_cadence_roundtrips(tmp_path, monkeypatch):
    monkeypatch.setenv("MOKO_HOME", str(tmp_path))
    config = cfg.Config(
        companion_name="Jordan",
        timezone="Asia/Tokyo",
        sleep_start="23:00",
        sleep_end="07:00",
        proactivity="moderate",
        cadence=cfg.Cadence(outreach_settle_minutes=45, reflect_muse_interval_minutes=15),
    )
    config.save()
    loaded = cfg.load()
    assert loaded.cadence.outreach_settle_minutes == 45
    assert loaded.cadence.reflect_muse_interval_minutes == 15


def test_chat_verbosity_roundtrips(tmp_path, monkeypatch):
    monkeypatch.setenv("MOKO_HOME", str(tmp_path))
    config = cfg.Config(
        companion_name="Jordan",
        timezone="Asia/Tokyo",
        sleep_start="23:00",
        sleep_end="07:00",
        proactivity="moderate",
        chat_verbosity="verbose",
    )
    config.save()
    assert cfg.load().chat_verbosity == "verbose"


# ── load-time validation: a hand-edited config.yaml can't crash a later beat ──
# from_dict clamps a bad value back to its default and logs a warning; it never raises.


def test_bad_proactivity_clamps_to_default(caplog):
    with caplog.at_level("WARNING"):
        loaded = cfg.Config.from_dict({"proactivity": "loud"})
    assert loaded.proactivity == "moderate"
    assert any("proactivity" in r.message for r in caplog.records)


def test_bad_timezone_falls_back_to_utc(caplog):
    with caplog.at_level("WARNING"):
        loaded = cfg.Config.from_dict({"timezone": "Mars/Olympus"})
    assert loaded.timezone == "UTC"
    assert any("timezone" in r.message for r in caplog.records)


def test_bad_sleep_window_falls_back_to_defaults(caplog):
    with caplog.at_level("WARNING"):
        loaded = cfg.Config.from_dict({"sleep": {"start": "99:99", "end": "nope"}})
    assert loaded.sleep_start == "23:00"
    assert loaded.sleep_end == "07:00"
    assert any("sleep" in r.message for r in caplog.records)


def test_valid_values_pass_through_unchanged():
    loaded = cfg.Config.from_dict(
        {
            "proactivity": "chatty",
            "timezone": "Asia/Tokyo",
            "sleep": {"start": "22:30", "end": "06:00"},
            "chat": {"verbosity": "verbose"},
            "language": "ja",
        }
    )
    assert loaded.proactivity == "chatty"
    assert loaded.timezone == "Asia/Tokyo"
    assert loaded.sleep_start == "22:30"
    assert loaded.sleep_end == "06:00"
    assert loaded.chat_verbosity == "verbose"
    assert loaded.language == "ja"
