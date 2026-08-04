"""Provider/modality catalog — the single source for which providers serve each modality's
default model, and which auth env var each provider's API key lives in.

The setup wizard and the `moko config` TUI each render their own (differently-styled) provider
*lists*; only the data that must agree — the default model ids and the env-var names — lives
here, so the two surfaces (and the doctor + the LLM layer) can't drift out of step.
"""

from __future__ import annotations

# Provider → the environment variable its API key lives in (the `env` auth method).
ENV_VARS = {
    "anthropic": "ANTHROPIC_API_KEY",
    "openai": "OPENAI_API_KEY",
    "xai": "XAI_API_KEY",
}

# Per-modality default model id, by provider. A provider absent from a modality's map has an
# endpoint that takes no model id (e.g. xAI /tts, /stt) — callers skip the model prompt for it.
MODALITY_DEFAULT_MODEL = {
    "image": {"openai": "gpt-image-1", "xai": "grok-imagine-image",
              "google": "gemini-2.5-flash-image"},
    "video": {"xai": "grok-imagine-video"},
    "speech": {"openai": "gpt-4o-mini-tts"},
    "hearing": {"openai": "whisper-1"},
}
