"""Anthropic Claude adapter — API key, or a Claude subscription via OAuth."""

from __future__ import annotations

import asyncio
import base64

import anthropic
from anthropic import AsyncAnthropic

from ..clock import now_utc
from . import claude_oauth
from .base import (
    LLM_MAX_RETRIES,
    LLM_TIMEOUT_S,
    ImagePart,
    LLMClient,
    LLMResponse,
    Msg,
    System,
    Tool,
    ToolExecutor,
)

_REFRESH_SKEW_MS = 120_000  # refresh a subscription token ~2 min before it expires


def _content(content: str | list) -> str | list:
    """Translate a provider-neutral message content into Anthropic blocks. A plain
    string passes through (the common case); a list of text + `ImagePart`s becomes
    text/image blocks (base64), so a photo rides in the turn the model already sees."""
    if isinstance(content, str):
        return content
    blocks: list[dict] = []
    for part in content:
        if isinstance(part, ImagePart):
            blocks.append({
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": part.mime,
                    "data": base64.b64encode(part.data).decode("ascii"),
                },
            })
        elif part:  # non-empty text
            blocks.append({"type": "text", "text": part if isinstance(part, str) else str(part)})
    return blocks


def _system_param(system: str | System, *, oauth: bool = False):
    """Render the system prompt for the API.

    A `System` becomes text blocks with a `cache_control` breakpoint on the
    stable prefix — which caches everything before it (the tools too) — and the
    volatile tail (the clock) trailing uncached. `System(cache=False)` skips the
    breakpoint entirely: a cache WRITE costs a ~25% premium, so a path that can
    never re-read its own prefix within the TTL shouldn't pay it. In OAuth
    (subscription) mode the Claude Code identity block is prepended; it's
    constant, so it stays inside the cached prefix and doesn't disturb caching."""
    prefix = [{"type": "text", "text": claude_oauth.SYSTEM_PREFIX}] if oauth else []
    if isinstance(system, System):
        stable: dict = {"type": "text", "text": system.stable}
        if system.cache:
            stable["cache_control"] = {"type": "ephemeral"}
        blocks = [stable]
        if system.volatile:
            blocks.append({"type": "text", "text": system.volatile})
        return prefix + blocks
    if oauth:
        return prefix + ([{"type": "text", "text": system}] if system else [])
    return system


class AnthropicClient(LLMClient):
    """Talks to the Messages API. With `api_key` it uses `x-api-key`; with
    `oauth` (the `claude` login's token) it uses Bearer + the Claude Code
    identity headers/prefix and refreshes the token in place near expiry."""

    def __init__(
        self, model: str, *, api_key: str | None = None, oauth: dict | None = None
    ) -> None:
        self.model = model
        self._api_key = api_key
        self._oauth = oauth  # {access_token, refresh_token, expires_at_ms} or None
        # Single-flight guard around _refresh: the refresh token is single-use, so two
        # concurrent callers that both exchanged it would burn the second exchange
        # (invalid_grant). The lock serializes them; a second waiter re-checks freshness
        # inside the lock and adopts the first's result instead of exchanging again.
        self._refresh_lock = asyncio.Lock()
        # Every current Claude model (claude-3 onward, incl. 4.x) can see images.
        self.supports_vision = True
        self._build()

    def _build(self) -> None:
        if self._oauth:
            self._client = AsyncAnthropic(
                auth_token=self._oauth["access_token"],
                default_headers=claude_oauth.headers(),
                timeout=LLM_TIMEOUT_S,
                max_retries=LLM_MAX_RETRIES,
            )
        else:
            self._client = AsyncAnthropic(
                api_key=self._api_key, timeout=LLM_TIMEOUT_S, max_retries=LLM_MAX_RETRIES
            )

    def _still_fresh(self, exp) -> bool:
        """True if `exp` (ms epoch) is beyond the refresh skew — a token good to use as-is."""
        return bool(exp) and now_utc().timestamp() * 1000 < exp - _REFRESH_SKEW_MS

    async def _refresh(self) -> None:
        if not self._oauth:
            return
        # Single-flight: hold the lock across the whole read-adopt-or-exchange so two
        # in-flight requests can't both spend the single-use refresh token.
        async with self._refresh_lock:
            # A second waiter arriving after the first already refreshed: our in-memory
            # token is now fresh (the first waiter adopted/exchanged under this same
            # lock). Adopt that result — don't re-read disk, don't exchange again.
            if self._still_fresh(self._oauth.get("expires_at_ms")):
                return
            # The shared credentials file is the source of truth. The `claude` CLI
            # rotates the single-use refresh token on ITS refreshes, so our in-memory
            # copy can be stale; re-read disk first. If the file already holds a still-
            # valid access token (the CLI just refreshed), adopt it — no network, no
            # spending our own refresh. Otherwise exchange the file's LATEST refresh
            # token, not our stale one, so we don't trigger an invalid_grant that would
            # silently stop Moko answering until a restart.
            latest = await asyncio.to_thread(claude_oauth.read_credentials)
            if latest and latest.get("accessToken") and self._still_fresh(latest.get("expiresAt")):
                self._oauth = {
                    "access_token": latest["accessToken"],
                    "refresh_token": latest.get("refreshToken"),
                    "expires_at_ms": latest["expiresAt"],
                }
                self._build()
                return
            token = (latest or {}).get("refreshToken") or self._oauth.get("refresh_token")
            if not token:
                return
            blob = await asyncio.to_thread(claude_oauth.refresh, token)
            self._oauth = {
                "access_token": blob["accessToken"],
                "refresh_token": blob.get("refreshToken"),
                "expires_at_ms": blob.get("expiresAt"),
            }
            self._build()

    async def _messages_create(self, **kwargs):
        """One Messages call, keeping a subscription token fresh: refresh
        proactively near expiry, and once reactively on a 401/403."""
        if self._oauth:
            exp = self._oauth.get("expires_at_ms")
            if exp and now_utc().timestamp() * 1000 >= exp - _REFRESH_SKEW_MS:
                await self._refresh()
        try:
            return await self._client.messages.create(**kwargs)
        except (anthropic.AuthenticationError, anthropic.PermissionDeniedError):
            if not self._oauth:
                raise
            await self._refresh()
            return await self._client.messages.create(**kwargs)

    async def complete(
        self,
        system: str | System,
        messages: list[Msg],
        *,
        max_tokens: int = 1024,
    ) -> LLMResponse:
        # No sampling params: current Claude models (Opus 4.7+) reject them with a 400.
        resp = await self._messages_create(
            model=self.model,
            system=_system_param(system, oauth=bool(self._oauth)),
            max_tokens=max_tokens,
            messages=[{"role": m.role, "content": _content(m.content)} for m in messages],
        )
        text = "".join(
            block.text for block in resp.content if getattr(block, "type", None) == "text"
        )
        usage = getattr(resp, "usage", None)
        return LLMResponse(
            text,
            input_tokens=getattr(usage, "input_tokens", None),
            output_tokens=getattr(usage, "output_tokens", None),
            cache_read_tokens=getattr(usage, "cache_read_input_tokens", None),
            cache_write_tokens=getattr(usage, "cache_creation_input_tokens", None),
        )

    async def converse(
        self,
        system: str | System,
        messages: list[Msg],
        *,
        tools: list[Tool],
        execute: ToolExecutor,
        max_tokens: int = 1024,
        max_steps: int = 6,
        tool_choice: str | None = None,
    ) -> LLMResponse:
        history: list[dict] = [{"role": m.role, "content": _content(m.content)} for m in messages]
        specs = [
            {"name": t.name, "description": t.description, "input_schema": t.parameters}
            for t in tools
        ]
        system_param = _system_param(system, oauth=bool(self._oauth))
        terminal = {t.name for t in tools if t.terminal}
        in_tokens = out_tokens = cache_read = cache_write = 0
        last_in = 0
        text = ""

        def _tally(resp) -> str:
            nonlocal in_tokens, out_tokens, cache_read, cache_write, last_in
            usage = getattr(resp, "usage", None)
            last_in = getattr(usage, "input_tokens", 0) or 0
            in_tokens += last_in
            out_tokens += getattr(usage, "output_tokens", 0) or 0
            cache_read += getattr(usage, "cache_read_input_tokens", 0) or 0
            cache_write += getattr(usage, "cache_creation_input_tokens", 0) or 0
            return "".join(b.text for b in resp.content if getattr(b, "type", None) == "text")

        # Force a named tool on the FIRST model call only, then revert to auto (see
        # the OpenAI adapter) — kept for interface parity; the forced path is the
        # weaker-brain (OpenAI/xAI) side in practice.
        force = tool_choice
        tool_calls: list[dict] = []  # every tool the model called this turn, for the ledger
        for _ in range(max_steps):
            # Omit `tools` when specs is empty (the lone terminal tool was dropped):
            # an empty array is provider-dependent, so don't rely on it — keep the
            # continuation tool-less, matching the OpenAI adapter.
            tool_kw = {"tools": specs} if specs else {}
            if tool_kw and force:
                tool_kw["tool_choice"] = {"type": "tool", "name": force}
            resp = await self._messages_create(
                model=self.model,
                system=system_param,
                max_tokens=max_tokens,  # no sampling params — Opus 4.7+ rejects them
                messages=history,
                **tool_kw,
            )
            force = None  # continuation steps choose freely
            text = _tally(resp)
            tool_uses = [b for b in resp.content if getattr(b, "type", None) == "tool_use"]
            if not tool_uses:
                break
            history.append({"role": "assistant", "content": resp.content})
            results = []
            for tu in tool_uses:
                args = dict(tu.input or {})
                tool_calls.append({"name": tu.name, "input": args})
                result = await execute(tu.name, args)
                results.append({"type": "tool_result", "tool_use_id": tu.id, "content": result})
            history.append({"role": "user", "content": results})
            # Reply already written + only write tools called → done, no extra call.
            if text.strip() and all(tu.name in terminal for tu in tool_uses):
                break
            # A terminal tool is once-per-turn: drop it so the model can't
            # re-emit the same write on the continuation (when it called the
            # write first and hasn't produced the reply text yet).
            done = {tu.name for tu in tool_uses if tu.name in terminal}
            if done:
                specs = [s for s in specs if s["name"] not in done]
        else:
            # Step budget exhausted mid-task: one last call WITHOUT tools so the
            # turn closes on an honest line ("here's where I got to") rather than
            # ending on a tool step with no reply — the old silent-truncation bug.
            final = await self._messages_create(
                model=self.model, system=system_param, max_tokens=max_tokens, messages=history,
            )
            text = _tally(final) or text
        return LLMResponse(
            text,
            input_tokens=in_tokens,
            output_tokens=out_tokens,
            cache_read_tokens=cache_read,
            cache_write_tokens=cache_write,
            last_input_tokens=last_in or None,
            tool_calls=tool_calls or None,
        )
