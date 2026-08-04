"""Shared presentation catalogues for the two settings surfaces — the first-run
wizard (:mod:`moko.cli.wizard`) and the live editor (:mod:`moko.cli.config_tui`).

Only the *display* data that both surfaces must agree on lives here: the provider
and model option lists, the friendly proactivity/interest labels, the curated
time presets, and the two tiny formatters. The data that the agent and doctor
also depend on (default model ids, env-var names) stays in
:mod:`moko.config.providers`; this module never duplicates it.
"""

from __future__ import annotations

from ..config import config as cfg
from ..config import providers as _providers

# Re-exported (single source stays providers): the sign-in flows need to know which
# env var each provider's key lives in to offer the "use my $VAR" option.
ENV_VARS = _providers.ENV_VARS

# Curated time options, so nobody has to think in 24-hour time.
SLEEP_TIMES = ["21:00", "21:30", "22:00", "22:30", "23:00", "23:30", "00:00", "00:30", "01:00"]
WAKE_TIMES = ["05:00", "05:30", "06:00", "06:30", "07:00", "07:30", "08:00", "08:30", "09:00"]

# (value, English label). Translations live in the i18n catalogue, keyed by value;
# these English strings are the fallback and what the config editor shows.
PROACTIVITY = [
    ("gentle", "Gentle — a quiet companion; I'll reach out only now and then"),
    ("moderate", "Moderate — I'll check in and get curious from time to time (recommended)"),
    ("chatty", "Chatty — I'll ask questions and check in more often"),
]

INTERESTS = [
    ("keeping_in_touch", "Keeping in touch with people I care about"),
    ("calendar", "Staying on top of my calendar and reminders"),
    ("remembering", "Remembering the important details of my life"),
    ("curious", "Being curious and learning about my world"),
    ("company", "Just being good company"),
]

# Moko's brain. BYO provider (Anthropic + OpenAI-compatible first).
LLM_PROVIDERS = [
    ("anthropic", "Anthropic Claude — recommended"),
    ("openai", "OpenAI"),
    ("xai", "xAI Grok"),
    ("openai_compatible", "Another provider (any OpenAI-compatible endpoint)"),
    ("none", "I'll decide later"),
]

ANTHROPIC_MODELS = [
    ("claude-opus-4-8", "Claude Opus 4.8 — the most capable (recommended)"),
    ("claude-sonnet-4-6", "Claude Sonnet 4.6 — a balance of speed and smarts"),
    ("claude-haiku-4-5-20251001", "Claude Haiku 4.5 — fast and light"),
]

# xAI is OpenAI-compatible; we fill the base URL in for the user.
XAI_BASE_URL = "https://api.x.ai/v1"
XAI_MODELS = [
    ("grok-4.3", "Grok 4.3 — general chat & coding (recommended)"),
    ("grok-4-1-fast-reasoning", "Grok 4.1 Fast — reasoning"),
    ("grok-code-fast-1", "Grok Code Fast — agentic coding"),
    ("__other__", "Something else (I'll type the model name)"),
]

PROVIDER_LABELS = {
    "anthropic": "Anthropic Claude",
    "openai": "OpenAI",
    "xai": "xAI Grok",
    "openai_compatible": "Custom (OpenAI-compatible)",
    "none": "not chosen yet",
}

AUTH_LABELS = {
    "claude_code": "via Claude Code on this machine",
    "oauth": "via Grok subscription (OAuth)",
    "api_key": "with an API key",
    "env": "from an environment variable",
}


def format_time_label(hhmm: str) -> str:
    """ "23:00" -> "11:00 PM", "00:30" -> "12:30 AM"."""
    hour, minute = (int(part) for part in hhmm.split(":"))
    suffix = "AM" if hour < 12 else "PM"
    hour12 = hour % 12 or 12
    return f"{hour12}:{minute:02d} {suffix}"


def brain_label(config: cfg.Config) -> str:
    """A friendly one-line description of Moko's configured brain."""
    if config.llm_provider == "none":
        return "not chosen yet — run [accent]moko setup[/accent]"
    parts = [PROVIDER_LABELS.get(config.llm_provider, config.llm_provider)]
    if config.llm_model:
        parts.append(config.llm_model)
    label = " · ".join(parts)
    auth = AUTH_LABELS.get(config.llm_auth_method)
    if auth:
        label += f"  ({auth})"
    return label
