"""Which models Moko will feed image bytes to (M10/M11).

Vision is model-specific: OpenAI flagships are multimodal; for xAI it's Grok 4.x + the
grok-2-vision line only (grok-3 / grok-2-1212 are text-only); unknown openai-compatible
endpoints stay off so a text-only endpoint never gets image bytes.
"""

from moko.llm import _vision_capable


def test_openai_vision_is_model_specific():
    for m in ("gpt-4o", "gpt-4.1", "gpt-4-turbo", "chatgpt-4o-latest", "o1", "o3", "o4-mini"):
        assert _vision_capable("openai", m) is True, m
    # Text-only OpenAI models a companion could realistically pick must NOT be mis-enabled.
    for m in ("o3-mini", "o1-mini", "gpt-3.5-turbo", "gpt-4-0314"):
        assert _vision_capable("openai", m) is False, m


def test_xai_vision_is_model_specific():
    for m in ("grok-4", "grok-4.3", "grok-2-vision-1212", "grok-2-vision", "grok-vision-beta"):
        assert _vision_capable("xai", m) is True, m
    for m in ("grok-3", "grok-3-mini", "grok-2-1212", "grok-beta"):
        assert _vision_capable("xai", m) is False, m


def test_unknown_compatible_endpoints_stay_text_only():
    assert _vision_capable("openai_compatible", "some-local-model") is False
    assert _vision_capable("xai", "") is False
