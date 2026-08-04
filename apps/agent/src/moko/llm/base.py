"""Provider-agnostic LLM interface.

Nothing above this layer knows which provider is in use; everything talks
to an ``LLMClient``. Adapters live alongside (anthropic_client.py,
openai_client.py) and are built by ``build_client`` (see __init__.py).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

# Per-request timeout for every provider call. Without it a stalled API call
# would block the daemon's single serial consumer indefinitely (no replies, no
# heartbeat). A timeout raises, which the gateway/consumer catches and recovers
# from on the next event/tick. The real worst-case stall is
# `timeout × (LLM_MAX_RETRIES + 1)` plus the SDK's backoff sleeps — both clients
# set max_retries explicitly so the bound is small and known, not the SDK default
# of 2 (which would make it ~3×).
LLM_TIMEOUT_S = 60.0
LLM_MAX_RETRIES = 1  # one retry for a transient 429/5xx — enough to ride a blip, not blast


@dataclass
class ImagePart:
    """An image inside a message's content, provider-neutral. The adapters translate
    it into the provider's image block; nothing above `llm/` knows the wire format.
    Vision is a *chat-model capability* — an image rides in the normal turn, it isn't
    a separate service (cf. TTS/image-gen)."""

    data: bytes
    mime: str = "image/jpeg"


@dataclass
class Msg:
    """One conversational turn, provider-neutral. `content` is text, or — for a
    multimodal turn — a list mixing text and `ImagePart`s (only sent when the model
    actually supports vision; see `LLMClient.supports_vision`)."""

    role: str  # "user" | "assistant"
    content: str | list[str | ImagePart]


@dataclass
class System:
    """A system prompt split into a stable prefix and a volatile tail.

    The stable part (persona + companion core + tool context) is identical
    turn to turn within a session; the volatile part is what changes each
    turn (the clock). Adapters cache the stable prefix where the provider
    supports it (Anthropic `cache_control`; OpenAI/xAI implicit prefix
    caching), so the bulk of the system prompt isn't re-billed every turn.
    Keep the volatile part LAST so the cacheable prefix stays contiguous.

    `cache=False` opts a call OUT of explicit caching (an Anthropic cache write costs a
    ~25% premium): right for paths that can never re-read their own prefix within the
    provider's cache TTL — e.g. the outreach consult, whose 10-minute reconsider floor
    outlives the ~5-minute TTL, so every write there was a premium with no future read.
    """

    stable: str
    volatile: str = ""
    cache: bool = True

    def text(self) -> str:
        """Flatten to a single string (for providers without explicit caching)."""
        return f"{self.stable}\n\n{self.volatile}" if self.volatile else self.stable


@dataclass
class Tool:
    """A tool the model may call. `parameters` is a JSON Schema object.

    `terminal` marks a write tool that ends the turn: when the model calls
    only terminal tools *and* has already produced its reply text in the
    same step, the converse loop runs the tool(s) and stops — no extra
    round-trip just to hear the model say nothing back. (Read tools like
    recall/lookup are non-terminal: the model needs their result to reply.)
    """

    name: str
    description: str
    parameters: dict
    terminal: bool = False


# Executes a tool call and returns the result text the model will read.
ToolExecutor = Callable[[str, dict], Awaitable[str]]


@dataclass
class LLMResponse:
    text: str
    input_tokens: int | None = None
    output_tokens: int | None = None
    # Prompt-cache accounting, when the provider reports it. `cache_read` is
    # input tokens served from cache (the win); `cache_write` is tokens
    # written to the cache this call (Anthropic only — a one-time ~25% cost).
    cache_read_tokens: int | None = None
    cache_write_tokens: int | None = None
    # The input tokens of the FINAL step only (converse). `input_tokens` sums
    # every step, which over-counts a multi-step turn's working-memory size; this
    # is the size of the last prompt — what session-size rotation should track.
    last_input_tokens: int | None = None
    # Every tool the model called this turn, in order — [{"name", "input"}] — so the
    # structured side of a turn (a `remember` payload, a `decide_outreach`) is visible in
    # the ledger/monitor, not just the free-text reply. None when no tools were used.
    tool_calls: list[dict] | None = None


class LLMClient(ABC):
    """The seam. Tests fake this; adapters implement it."""

    model: str
    # Whether the configured model can see images. Set by the adapter/`build_client`
    # from the provider; the brain only sends image content when this is True, and
    # otherwise tells Moko honestly it received a file it can't see. Defaults off so
    # an unknown provider never gets fed image bytes it can't read.
    supports_vision: bool = False
    # Whether this brain benefits from explicit scaffolding — a weaker model that
    # doesn't reliably emit tool calls or infer the persona's voice on its own. Set
    # by `build_client` per provider (frontier models: False). The brain reads it to
    # force the `remember` tool in salvage and to add a short voice note — so the
    # capable models' prompt and behaviour stay untouched. Mirrors supports_vision.
    needs_scaffolding: bool = False

    @abstractmethod
    async def complete(
        self,
        system: str | System,
        messages: list[Msg],
        *,
        max_tokens: int = 1024,
    ) -> LLMResponse: ...

    @abstractmethod
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
        """Run a tool-use loop: the model may call `tools` (run via
        `execute`), looped up to `max_steps`, returning its final reply.
        `tool_choice`, when set to a tool name, forces that tool on the FIRST
        step only (then reverts to free choice) — for weaker brains that won't
        reliably call it on their own.

        A step that calls only `terminal` tools and already carries reply
        text ends the loop after running them — see `Tool.terminal`."""


def estimate_tokens(text: str) -> int:
    """Rough token count (~4 chars/token) for size-based session rotation.

    Real providers return exact usage; this is the fallback when they
    don't, and is plenty for deciding when to start a fresh session.
    """
    return max(1, len(text) // 4)
