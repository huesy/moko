"""LLM usage metering — record every call to the brain model, in one place.

Each provider already reports exact token usage on its `LLMResponse`; what was missing
was *recording* it. Rather than sprinkle bookkeeping across the dozen call sites, we wrap
the `LLMClient` once (in `Brain.__init__`, so the wrapped instance flows to the studio too)
with `UsageMeter`, which writes one `llm_calls` row per call. What the
call *was* — reply, vision, reflection, dream, outreach, research — rides in on a
`contextvar` the calling code sets right before the call; the meter reads it and labels the
row.

The meter also captures the **exact I/O** of every call — the system prompt, the request
messages, and the reply — and announces each call on the live feed (`llm.call`), so the
monitor can show 100% of what goes in and out of the model. (It captures a turn's bookend:
the request in + the final reply out; the intermediate tool steps of a tool-use turn surface
as their own feed events.)
"""

from __future__ import annotations

import contextlib
import contextvars
import json

from ..events import publish as _publish_event
from ..llm import LLMClient, LLMResponse, System
from ..log import get_logger
from ..store import Store

_log = get_logger("usage")

_MAX_IO = 100_000  # cap the system prompt / reply field, so one pathological call can't bloat a row
_MAX_MSG = 12_000  # cap each request message's text (bounds the request AND keeps it valid JSON)
LLM_IO_RETENTION_DAYS = 14  # keep verbatim I/O this long; older rows keep token counts only

# What the next LLM call is *for*. Set right before a call (see brain/dream); the
# meter reads it. "other" is the honest catch-all for any call that didn't label itself.
usage_kind: contextvars.ContextVar[str] = contextvars.ContextVar("usage_kind", default="other")


def _system_text(system: object) -> str:
    """The system prompt as a single string (a `System` flattens its cached prefix + tail)."""
    return system.text() if isinstance(system, System) else str(system)


def _tools_json(calls: list[dict] | None) -> str | None:
    """The turn's tool calls as bounded JSON — [{"name", "input"}] — or None if the model
    used none. The structured half of the response (a `remember` payload, a `decide_outreach`),
    so the monitor shows the FULL I/O, not just the reply text."""
    if not calls:
        return None
    return json.dumps(calls, ensure_ascii=False)[:_MAX_IO]


def _messages_json(messages: object) -> str:
    """Flatten the request messages to JSON (role + text), images noted as `[image]` rather
    than dumping bytes — what was actually sent, readable in the monitor. Each message's text
    is capped (and the whole list bounded), so the result stays *valid JSON* and can't bloat a
    row — unlike slicing the serialized string afterwards, which would corrupt it."""
    out: list[dict] = []
    budget = _MAX_IO
    for m in messages or []:
        content = getattr(m, "content", m)
        if isinstance(content, list):
            text = " ".join(p if isinstance(p, str) else "[image]" for p in content)
        else:
            text = str(content)
        text = text[:_MAX_MSG]
        out.append({"role": getattr(m, "role", "?"), "text": text})
        budget -= len(text)
        if budget <= 0:  # overall guard across many messages
            break
    return json.dumps(out, ensure_ascii=False)


class UsageMeter(LLMClient):
    """Wraps an `LLMClient`, forwarding every call unchanged while recording its token usage
    + exact I/O to the `llm_calls` ledger and announcing it on the feed. Best-effort: a failed
    write/publish is swallowed so bookkeeping can never break a turn."""

    def __init__(self, inner: LLMClient, store: Store) -> None:
        self._inner = inner
        self._store = store
        # Mirror the bits callers read off the client directly, so the wrapper is transparent.
        self.model = getattr(inner, "model", "")
        self.supports_vision = getattr(inner, "supports_vision", False)
        self.needs_scaffolding = getattr(inner, "needs_scaffolding", False)

    async def complete(self, system, messages, **kwargs) -> LLMResponse:
        resp = await self._inner.complete(system, messages, **kwargs)
        await self._record(system, messages, resp)
        return resp

    async def converse(self, system, messages, **kwargs) -> LLMResponse:
        resp = await self._inner.converse(system, messages, **kwargs)
        await self._record(system, messages, resp)
        return resp

    async def _record(self, system, messages, resp: LLMResponse) -> None:
        kind = usage_kind.get()
        with contextlib.suppress(Exception):
            rid = await self._store.usage.add_llm_call(
                kind=kind,
                model=self.model,
                input_tokens=resp.input_tokens or 0,
                output_tokens=resp.output_tokens or 0,
                cache_read_tokens=resp.cache_read_tokens or 0,
                cache_write_tokens=resp.cache_write_tokens or 0,
                system_prompt=_system_text(system)[:_MAX_IO],
                request=_messages_json(messages),  # already bounded + valid JSON (per-message)
                response=(resp.text or "")[:_MAX_IO],
                tool_calls=_tools_json(resp.tool_calls),  # the structured side of the turn
            )
            # Announce EVERY call on the live feed; the `id` deep-links to the full I/O detail.
            _publish_event({
                "type": "llm.call",
                "id": rid,
                "kind": kind,
                "model": self.model,
                "input_tokens": resp.input_tokens,
                "output_tokens": resp.output_tokens,
                "cache_read_tokens": resp.cache_read_tokens,
                "preview": (resp.text or "").strip()[:160],
                "tools": [c.get("name") for c in (resp.tool_calls or [])],  # names, for the feed
            })
