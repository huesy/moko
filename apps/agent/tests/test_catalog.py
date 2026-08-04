"""The shared presentation catalogue — provider/model lists, time presets, and the
two formatters that the wizard and the config editor both render from."""

from moko.cli import catalog
from moko.config.config import Config


def test_format_time_label():
    assert catalog.format_time_label("23:00") == "11:00 PM"
    assert catalog.format_time_label("00:30") == "12:30 AM"
    assert catalog.format_time_label("07:00") == "7:00 AM"
    assert catalog.format_time_label("12:00") == "12:00 PM"
    assert catalog.format_time_label("13:05") == "1:05 PM"


def test_time_option_lists_are_valid():
    for value in catalog.SLEEP_TIMES + catalog.WAKE_TIMES:
        hour, minute = value.split(":")
        assert 0 <= int(hour) < 24
        assert int(minute) in (0, 30)


def test_proactivity_has_moderate_default():
    assert "moderate" in [value for value, _ in catalog.PROACTIVITY]


def test_interests_values_are_unique():
    values = [value for value, _ in catalog.INTERESTS]
    assert len(values) == len(set(values))


def test_llm_provider_values_are_unique_and_cover_expected():
    values = [value for value, _ in catalog.LLM_PROVIDERS]
    assert len(values) == len(set(values))
    assert {"anthropic", "xai", "none"} <= set(values)


def test_xai_base_url_and_models():
    assert catalog.XAI_BASE_URL == "https://api.x.ai/v1"
    values = [value for value, _ in catalog.XAI_MODELS]
    assert "grok-4.3" in values
    assert "__other__" in values  # always an escape hatch for new models


def test_env_vars_reexported_from_providers():
    assert catalog.ENV_VARS["anthropic"] == "ANTHROPIC_API_KEY"
    assert catalog.ENV_VARS["openai"] == "OPENAI_API_KEY"


def test_brain_label_not_chosen():
    assert "not chosen yet" in catalog.brain_label(Config.from_dict({}))


def test_brain_label_with_provider_and_model():
    config = Config(
        companion_name="A", timezone="UTC", sleep_start="23:00", sleep_end="07:00",
        proactivity="moderate", llm_provider="anthropic", llm_model="claude-opus-4-8",
        llm_auth_method="claude_code",
    )
    label = catalog.brain_label(config)
    assert "Anthropic Claude" in label
    assert "claude-opus-4-8" in label
    assert "Claude Code" in label


def test_brain_label_xai_oauth():
    config = Config(
        companion_name="A", timezone="UTC", sleep_start="23:00", sleep_end="07:00",
        proactivity="moderate", llm_provider="xai", llm_model="grok-4.3",
        llm_auth_method="oauth",
    )
    label = catalog.brain_label(config)
    assert "xAI Grok" in label
    assert "grok-4.3" in label
    assert "OAuth" in label
