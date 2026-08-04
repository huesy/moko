"""The LLM layer — one interface, several providers.

`build_client` turns the setup-time choice (provider/model/auth) into a
ready `LLMClient`, or raises `UnsupportedAuth` (with a friendly message)
for a provider/auth mismatch or missing credentials.
"""

from __future__ import annotations

import os

from ..config import secrets
from ..config.config import Config
from ..config.providers import ENV_VARS
from .base import (
    ImagePart,
    LLMClient,
    LLMResponse,
    Msg,
    System,
    Tool,
    ToolExecutor,
    estimate_tokens,
)

__all__ = [
    "LLMClient",
    "LLMResponse",
    "ImagePart",
    "Msg",
    "System",
    "Tool",
    "ToolExecutor",
    "estimate_tokens",
    "build_client",
    "UnsupportedAuth",
]

_XAI_BASE_URL = "https://api.x.ai/v1"


class UnsupportedAuth(Exception):
    """Raised when the configured sign-in method isn't wired up yet."""


def build_client(config: Config) -> LLMClient | None:
    """Construct an LLMClient from config + stored secrets.

    Returns None if no provider is chosen. Raises UnsupportedAuth for a
    provider/auth mismatch (e.g. Claude Code with a non-Anthropic provider)
    or when the required credentials are missing.
    """
    provider = config.llm_provider
    if provider == "none" or not provider:
        return None

    auth = config.llm_auth_method
    if auth == "claude_code":
        if provider != "anthropic":
            raise UnsupportedAuth("Claude Code sign-in only applies to the Anthropic provider.")
        from . import claude_oauth
        from .anthropic_client import AnthropicClient

        creds = claude_oauth.read_credentials()
        if not creds:
            raise UnsupportedAuth(
                "No Claude Code login found. Sign in with the `claude` CLI first "
                "(run `claude` and log in, or `claude setup-token`)."
            )
        return AnthropicClient(
            config.llm_model,
            oauth={
                "access_token": creds["accessToken"],
                "refresh_token": creds.get("refreshToken"),
                "expires_at_ms": creds.get("expiresAt"),
            },
        )
    if auth == "oauth":
        if provider != "xai":
            raise UnsupportedAuth("OAuth sign-in is only supported for xAI Grok right now.")
        return _build_xai_oauth(config)

    if auth == "env":
        env_var = ENV_VARS.get(provider)
        key = os.environ.get(env_var) if env_var else None
    else:  # api_key
        key = secrets.get_api_key(provider)
    if not key:
        raise UnsupportedAuth(f"No API key found for {provider}. Re-run `moko setup` to add one.")

    if provider == "anthropic":
        from .anthropic_client import AnthropicClient

        return AnthropicClient(config.llm_model, api_key=key)

    # openai, xai, openai_compatible all speak the OpenAI protocol.
    from .openai_client import OpenAIClient

    base_url = config.llm_base_url or (_XAI_BASE_URL if provider == "xai" else None)
    # OpenAI's flagships are multimodal; xai/openai-compatible vary, so default them
    # off (a text-only endpoint must never be fed image bytes). Per-modality config
    # will let the companion override this when M10's `models.vision` lands.
    # Frontier OpenAI is reliable at tool-use and infers the persona's voice; xAI
    # (Grok, observed weak at extraction) and unknown openai-compatible/local
    # endpoints get scaffolding (forced `remember`, an explicit voice note).
    return OpenAIClient(
        config.llm_model, key, base_url=base_url or None,
        supports_vision=_vision_capable(provider, config.llm_model),
        needs_scaffolding=provider != "openai",
    )


# Models that accept image INPUT — vision is model-specific, NOT provider-wide (a text-only
# model fed image bytes 400s and breaks the honesty floor). Unknown openai-compatible/local
# endpoints stay off. Confirmed against OpenAI + xAI (docs.x.ai) docs.
_XAI_VISION_PREFIXES = ("grok-4", "grok-2-vision", "grok-vision")
_OPENAI_VISION_PREFIXES = ("gpt-4o", "gpt-4.1", "gpt-4-turbo", "chatgpt-4o", "o1", "o3", "o4")
_OPENAI_TEXT_ONLY = ("o1-mini", "o3-mini")  # multimodal-family models that are NOT vision


def _vision_capable(provider: str, model: str) -> bool:
    m = (model or "").lower()
    if provider == "openai":
        if any(m.startswith(t) for t in _OPENAI_TEXT_ONLY):
            return False  # the mini reasoning models don't see images
        return any(m.startswith(p) for p in _OPENAI_VISION_PREFIXES)
    if provider == "xai":
        return any(m.startswith(p) for p in _XAI_VISION_PREFIXES)
    return False


def _build_xai_oauth(config: Config) -> LLMClient:
    """Use a stored Grok OAuth token as the bearer, kept fresh for the life of a
    long-running daemon (the client refreshes it just before a call near expiry, and
    once reactively if a call still 401/403s). The OpenAI client sends the key as
    `Authorization: Bearer …`, so an OAuth access token plugs straight into the xAI
    endpoint. The token resolution + refresh is shared with image gen (see
    `xai_auth.resolve_xai_oauth`)."""
    from .openai_client import OpenAIClient
    from .xai_auth import resolve_xai_oauth

    pair = resolve_xai_oauth()
    if pair is None:
        raise UnsupportedAuth("Connect your Grok subscription first: run `moko login`.")
    token, refresh = pair
    # The token is a generic-plan grant (see login.py's plan=generic), so it
    # bills through the standard API host — no version-gated proxy needed.
    return OpenAIClient(
        config.llm_model, token, base_url=_XAI_BASE_URL, refresh=refresh,
        # Grok 4.x / grok-2-vision can see images; grok-3 can't.
        supports_vision=_vision_capable("xai", config.llm_model),
        needs_scaffolding=True,  # Grok: scaffold tool-use + voice
    )
