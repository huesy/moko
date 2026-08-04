"""Per-modality model config (M10): the unified `models.{vision,image,video,speech,
hearing}` schema, its round-trip + back-compat, the `moko config` editing surface, and
that image generation is built from it."""

from moko.config import secrets
from moko.config.config import MODALITIES, Config, ModelChoice
from moko.media import build_image_client


def _cfg(**kw) -> Config:
    base = dict(companion_name="A", timezone="UTC", sleep_start="23:00",
                sleep_end="07:00", proactivity="moderate")
    base.update(kw)
    return Config(**base)


def test_modalities_are_the_five_extra_senses():
    assert set(MODALITIES) == {"vision", "image", "video", "speech", "hearing"}


def test_models_round_trip_and_unset_is_empty():
    c = _cfg()
    c.models["image"] = ModelChoice(provider="openai", model="gpt-image-1", auth="api_key")
    c2 = Config.from_dict(c.to_dict())
    img = c2.model_for("image")
    assert img.provider == "openai" and img.model == "gpt-image-1" and img.configured
    # Every modality slot exists after a round-trip; unconfigured ones are empty.
    assert set(MODALITIES) <= set(c2.models)
    assert not c2.model_for("speech").configured


def test_back_compat_old_image_block_loads():
    # The first-slice config wrote models.image.{provider,model}; it must still load.
    old = {"companion": {"name": "A"}, "timezone": "UTC",
           "models": {"image": {"provider": "openai", "model": "gpt-image-1"}}}
    assert Config.from_dict(old).model_for("image").provider == "openai"


def test_language_round_trips_and_defaults_to_english():
    assert _cfg().language == "en"  # the default when never set
    assert Config.from_dict(_cfg(language="ja").to_dict()).language == "ja"
    # A config written before the language field existed loads as English, not a crash.
    assert Config.from_dict({"companion": {"name": "A"}, "timezone": "UTC"}).language == "en"


def test_model_for_never_keyerrors_on_unset():
    assert _cfg().model_for("hearing").provider == "none"  # empty, not a crash


def test_build_image_client_from_models(tmp_path, monkeypatch):
    monkeypatch.setenv("MOKO_HOME", str(tmp_path))
    # Configured but no key → capability honestly off.
    c = _cfg()
    c.models["image"] = ModelChoice(provider="openai", model="gpt-image-1", auth="api_key")
    assert build_image_client(c) is None
    # With a stored key → a real client.
    secrets.set_api_key("openai", "sk-test")
    client = build_image_client(c)
    assert client is not None and client.model == "gpt-image-1"
    # Unconfigured → None.
    assert build_image_client(_cfg()) is None


# ── the `moko config` editing surface ─────────────────────────────────────────
def test_config_cli_sets_and_reads_modality_models():
    from moko.cli.config import _SPECS

    c = _cfg()
    _SPECS["models.image.provider"].set(c, "openai")
    _SPECS["models.image.model"].set(c, "gpt-image-1")
    assert c.model_for("image").provider == "openai"
    assert _SPECS["models.image.provider"].get(c) == "openai"
    assert _SPECS["models.image.model"].get(c) == "gpt-image-1"
    # Every modality is editable.
    for cap in MODALITIES:
        assert f"models.{cap}.provider" in _SPECS


def test_config_cli_rejects_unknown_provider():
    import pytest

    from moko.cli.config import _SPECS

    with pytest.raises(ValueError):
        _SPECS["models.image.provider"].set(_cfg(), "definitely-not-a-provider")


# ── the config TUI (config_tui.py) ────────────────────────────────────────────
def test_tui_has_senses_and_every_modality():
    from moko.cli import config_tui

    cats = {c.name: c for c in config_tui._build_model()}
    assert "Senses" in cats and "Telegram" in cats
    senses = {f.key for f in cats["Senses"].fields}
    assert {"vision", "image", "speech", "hearing", "portrait_ref"} <= senses


def test_tui_modality_editor_persists_provider_model_and_key(tmp_path, monkeypatch):
    monkeypatch.setenv("MOKO_HOME", str(tmp_path))
    from moko.cli import config_tui
    from moko.config import secrets

    c = _cfg()
    ed = config_tui.ModalityEditor(c, "image")
    assert ed.step == "provider"
    assert ed._advance("openai") is None and ed.step == "model"
    assert ed._advance("gpt-image-1") is None and ed.step == "auth"
    assert ed._advance("api_key") is None and ed.step == "key"
    assert ed._advance("sk-secret") == ("commit", None)  # final step persists

    assert c.model_for("image").provider == "openai"
    assert c.model_for("image").model == "gpt-image-1"
    assert secrets.get_api_key("openai") == "sk-secret"  # key went to secrets, not config


def test_tui_modality_editor_none_clears_the_slot(tmp_path, monkeypatch):
    monkeypatch.setenv("MOKO_HOME", str(tmp_path))
    from moko.cli import config_tui

    c = _cfg()
    c.models["speech"] = ModelChoice(provider="elevenlabs", model="x", auth="api_key")
    ed = config_tui.ModalityEditor(c, "speech")
    assert ed._advance("none") == ("commit", None)
    assert not c.model_for("speech").configured  # cleared


def test_tui_modality_editor_reuses_stored_key(tmp_path, monkeypatch):
    monkeypatch.setenv("MOKO_HOME", str(tmp_path))
    from moko.cli import config_tui
    from moko.config import secrets

    secrets.set_api_key("openai", "already-here")
    c = _cfg()
    c.models["image"] = ModelChoice(provider="openai", model="gpt-image-1", auth="api_key")
    ed = config_tui.ModalityEditor(c, "image")
    assert ed._advance("openai") is None  # provider → model
    # Same provider with a stored key → skip auth/key entirely.
    assert ed._advance("gpt-image-1") == ("commit", None)


def test_tui_modality_editor_does_not_reuse_when_key_is_missing(tmp_path, monkeypatch):
    monkeypatch.setenv("MOKO_HOME", str(tmp_path))
    from moko.cli import config_tui

    c = _cfg()
    # auth says api_key, but NO key is stored (e.g. it was deleted). Must NOT shortcut
    # to commit — that would persist auth=api_key with no key (a runtime auth failure).
    c.models["image"] = ModelChoice(provider="openai", model="gpt-image-1", auth="api_key")
    ed = config_tui.ModalityEditor(c, "image")
    assert ed._advance("openai") is None  # → model
    assert ed._advance("gpt-image-1") is None and ed.step == "auth"  # goes to auth, not commit


def test_tui_modality_editor_keeps_saved_key_when_switching_providers(tmp_path, monkeypatch):
    # The reported bug: a Google key is on file, the slot currently points at xAI, and the
    # user switches back to Google. The auth menu must offer "keep" (don't force a re-paste)
    # and choosing it must reuse the stored key without overwriting it.
    monkeypatch.setenv("MOKO_HOME", str(tmp_path))
    from moko.cli import config_tui
    from moko.config import secrets

    secrets.set_api_key("google", "google-key-from-before")
    c = _cfg()
    c.models["image"] = ModelChoice(provider="xai", model="grok-imagine-image", auth="oauth")
    overwrites: list[tuple] = []
    monkeypatch.setattr(secrets, "set_api_key", lambda p, k: overwrites.append((p, k)))

    ed = config_tui.ModalityEditor(c, "image")
    assert ed._advance("google") is None  # provider → model
    assert ed._advance("gemini-2.5-flash-image") is None and ed.step == "auth"  # not a shortcut
    assert "keep" in [v for _, v in ed.sub.choices]  # the no-friction option is offered
    assert ed._advance("keep") == ("commit", None)
    assert c.model_for("image").provider == "google"
    assert c.model_for("image").auth == "api_key"
    assert overwrites == []  # the stored Google key was reused, not rewritten


def test_tui_modality_editor_blank_key_keeps_saved_one(tmp_path, monkeypatch):
    # Choosing "Paste a different API key" then submitting blank also keeps the saved key.
    monkeypatch.setenv("MOKO_HOME", str(tmp_path))
    from moko.cli import config_tui
    from moko.config import secrets

    secrets.set_api_key("google", "google-key-from-before")
    c = _cfg()
    c.models["image"] = ModelChoice(provider="xai", model="grok-imagine-image", auth="oauth")
    overwrites: list[tuple] = []
    monkeypatch.setattr(secrets, "set_api_key", lambda p, k: overwrites.append((p, k)))

    ed = config_tui.ModalityEditor(c, "image")
    ed._advance("google")
    ed._advance("gemini-2.5-flash-image")
    assert ed._advance("api_key") is None and ed.step == "key"
    assert ed.sub.validate is None  # blank allowed because a key is already stored
    assert ed._advance("") == ("commit", None)  # blank → keep
    assert c.model_for("image").auth == "api_key"
    assert overwrites == []
