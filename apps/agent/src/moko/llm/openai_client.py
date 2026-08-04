"""OpenAI + OpenAI-compatible adapter (also serves xAI Grok via base_url)."""

from __future__ import annotations

import base64
import json
from collections.abc import Awaitable, Callable

from openai import AsyncOpenAI, AuthenticationError, PermissionDeniedError

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


def _content(content: str | list) -> str | list:
    """Translate a provider-neutral message content into the OpenAI chat format. A
    string passes through; a list of text + `ImagePart`s becomes the vision content
    array (text parts + `image_url` data URIs)."""
    if isinstance(content, str):
        return content
    parts: list[dict] = []
    for part in content:
        if isinstance(part, ImagePart):
            b64 = base64.b64encode(part.data).decode("ascii")
            parts.append({
                "type": "image_url",
                "image_url": {"url": f"data:{part.mime};base64,{b64}"},
            })
        elif part:
            parts.append({"type": "text", "text": part if isinstance(part, str) else str(part)})
    return parts


def _system_text(system: str | System) -> str:
    """Flatten the system prompt to a string. OpenAI/xAI cache stable prefixes
    implicitly, so keeping the volatile clock LAST (as `System` does) is all
    that's needed — the persona/tool prefix is reused automatically."""
    return system.text() if isinstance(system, System) else system


def _cached_tokens(usage) -> int:
    """Prompt tokens served from cache, if the provider reports them
    (`prompt_tokens_details.cached_tokens`). These still count in
    `prompt_tokens`; the cache just bills them at a discount."""
    details = getattr(usage, "prompt_tokens_details", None)
    return (getattr(details, "cached_tokens", 0) or 0) if details else 0


class OpenAIClient(LLMClient):
    def __init__(
        self,
        model: str,
        api_key: str,
        *,
        base_url: str | None = None,
        default_headers: dict[str, str] | None = None,
        refresh: Callable[[bool], Awaitable[str]] | None = None,
        supports_vision: bool = False,
        needs_scaffolding: bool = False,
    ) -> None:
        self.model = model
        self.needs_scaffolding = needs_scaffolding
        self.base_url = base_url  # the configured endpoint (e.g. xAI's host)
        # Vision varies by provider/model here (this client serves openai, xai, and
        # openai-compatible endpoints), so the caller decides — default off, since a
        # text-only compatible endpoint must never be fed image bytes.
        self.supports_vision = supports_vision
        # For OAuth: an async hook returning a current bearer (refreshing when
        # near expiry, or forced after an auth error). None for a static API key.
        self._refresh = refresh
        self._client = AsyncOpenAI(
            api_key=api_key,
            base_url=base_url,
            default_headers=default_headers,
            timeout=LLM_TIMEOUT_S,
            max_retries=LLM_MAX_RETRIES,
        )

    async def _create(self, **kwargs):
        """One chat completion, keeping the bearer fresh: refresh proactively
        before the call (OAuth tokens expire ~hourly), and once reactively on an
        auth error. It's a single request, so retrying is safe inside the tool
        loop — it never replays tool calls."""
        if self._refresh is not None:
            self._client.api_key = await self._refresh(False)
        try:
            return await self._client.chat.completions.create(**kwargs)
        except (AuthenticationError, PermissionDeniedError):
            if self._refresh is None:
                raise
            self._client.api_key = await self._refresh(True)
            return await self._client.chat.completions.create(**kwargs)

    async def complete(
        self,
        system: str | System,
        messages: list[Msg],
        *,
        max_tokens: int = 1024,
    ) -> LLMResponse:
        chat = [{"role": "system", "content": _system_text(system)}]
        chat += [{"role": m.role, "content": _content(m.content)} for m in messages]
        resp = await self._create(
            model=self.model,
            messages=chat,
            max_tokens=max_tokens,
        )
        text = resp.choices[0].message.content or ""
        usage = getattr(resp, "usage", None)
        return LLMResponse(
            text,
            input_tokens=getattr(usage, "prompt_tokens", None),
            output_tokens=getattr(usage, "completion_tokens", None),
            cache_read_tokens=_cached_tokens(usage) or None,
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
        chat: list[dict] = [{"role": "system", "content": _system_text(system)}]
        chat += [{"role": m.role, "content": _content(m.content)} for m in messages]
        specs = [
            {
                "type": "function",
                "function": {
                    "name": t.name,
                    "description": t.description,
                    "parameters": t.parameters,
                },
            }
            for t in tools
        ]
        terminal = {t.name for t in tools if t.terminal}
        in_tokens = out_tokens = cache_read = 0
        last_in = 0
        text = ""

        def _tally(resp) -> str:
            nonlocal in_tokens, out_tokens, cache_read, last_in
            usage = getattr(resp, "usage", None)
            last_in = getattr(usage, "prompt_tokens", 0) or 0
            in_tokens += last_in
            out_tokens += getattr(usage, "completion_tokens", 0) or 0
            cache_read += _cached_tokens(usage)
            return resp.choices[0].message.content or ""

        # Force a named tool on the FIRST model call only, then revert to auto, so a
        # caller that needs `remember` reliably called (weak brains) gets it without
        # the loop re-forcing the same call forever.
        force = tool_choice
        tool_calls: list[dict] = []  # every tool the model called this turn, for the ledger
        for _ in range(max_steps):
            # Never send an empty tools array: once the lone terminal tool is
            # dropped (below), specs can be []; OpenAI/xAI reject `tools: []` with a
            # 400, which used to kill outreach/reflection on the OpenAI path. Omit
            # the key entirely when there's nothing to offer.
            tool_kw = {"tools": specs} if specs else {}
            if tool_kw and force:
                tool_kw["tool_choice"] = {"type": "function", "function": {"name": force}}
            resp = await self._create(
                model=self.model,
                messages=chat,
                max_tokens=max_tokens,
                **tool_kw,
            )
            force = None  # continuation steps choose freely
            text = _tally(resp)
            msg = resp.choices[0].message
            if not msg.tool_calls:
                break
            chat.append(
                {
                    "role": "assistant",
                    "content": msg.content,
                    "tool_calls": [
                        {
                            "id": tc.id,
                            "type": "function",
                            "function": {
                                "name": tc.function.name,
                                "arguments": tc.function.arguments,
                            },
                        }
                        for tc in msg.tool_calls
                    ],
                }
            )
            for tc in msg.tool_calls:
                try:
                    args = json.loads(tc.function.arguments or "{}")
                except ValueError:
                    args = {}
                tool_calls.append({"name": tc.function.name, "input": args})
                result = await execute(tc.function.name, args)
                chat.append({"role": "tool", "tool_call_id": tc.id, "content": result})
            # Reply already written + only write tools called → done, no extra call.
            if text.strip() and all(tc.function.name in terminal for tc in msg.tool_calls):
                break
            # A terminal tool is once-per-turn: drop it so the model can't
            # re-emit the same write on the continuation (when it called the
            # write first and hasn't produced the reply text yet).
            done = {tc.function.name for tc in msg.tool_calls if tc.function.name in terminal}
            if done:
                specs = [s for s in specs if s["function"]["name"] not in done]
        else:
            # Step budget exhausted mid-task: one last call WITHOUT tools so the
            # turn closes on an honest line rather than ending on a tool step with
            # no reply (the silent-truncation bug).
            final = await self._create(
                model=self.model, messages=chat, max_tokens=max_tokens,
            )
            text = _tally(final) or text
        return LLMResponse(
            text,
            input_tokens=in_tokens,
            output_tokens=out_tokens,
            cache_read_tokens=cache_read or None,
            last_input_tokens=last_in or None,
            tool_calls=tool_calls or None,
        )
