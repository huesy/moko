import base64

import pytest

from moko.config import secrets
from moko.config.config import Config
from moko.llm import ImagePart, UnsupportedAuth, build_client, estimate_tokens
from moko.llm.anthropic_client import AnthropicClient, _system_param
from moko.llm.anthropic_client import _content as _anthropic_content
from moko.llm.base import Msg, System, Tool
from moko.llm.openai_client import OpenAIClient, _cached_tokens, _system_text
from moko.llm.openai_client import _content as _openai_content


# ── vision: provider-neutral image content → each wire format ──────────────────
def test_anthropic_translates_text_and_image_blocks():
    assert _anthropic_content("just text") == "just text"  # the common path is untouched
    blocks = _anthropic_content(["look at this", ImagePart(b"PNGBYTES", "image/png")])
    assert blocks[0] == {"type": "text", "text": "look at this"}
    img = blocks[1]
    assert img["type"] == "image" and img["source"]["media_type"] == "image/png"
    assert base64.b64decode(img["source"]["data"]) == b"PNGBYTES"


def test_openai_translates_to_vision_content_array():
    assert _openai_content("just text") == "just text"
    parts = _openai_content(["see this", ImagePart(b"JPG", "image/jpeg")])
    assert parts[0] == {"type": "text", "text": "see this"}
    assert parts[1]["type"] == "image_url"
    expect = "data:image/jpeg;base64," + base64.b64encode(b"JPG").decode()
    assert parts[1]["image_url"]["url"] == expect


def test_vision_capability_defaults_are_honest():
    # OpenAI-protocol clients default vision OFF (a text-only compatible endpoint must
    # never be fed image bytes); build_client turns it on only for the openai provider.
    assert OpenAIClient("m", "k").supports_vision is False
    assert OpenAIClient("m", "k", supports_vision=True).supports_vision is True


def _cfg(**kw) -> Config:
    base = dict(
        companion_name="A",
        timezone="UTC",
        sleep_start="23:00",
        sleep_end="07:00",
        proactivity="moderate",
    )
    base.update(kw)
    return Config(**base)


def test_estimate_tokens():
    assert estimate_tokens("") == 1
    assert estimate_tokens("a" * 40) == 10


# --- converse tool-loop behaviour (terminal vs read tools) -----------------
# Minimal stand-ins for the OpenAI SDK response shape, so we can drive the
# loop without a network call and count how many model round-trips it makes.


class _Fn:
    def __init__(self, name, args):
        self.name = name
        self.arguments = args


class _TC:
    def __init__(self, tc_id, name, args="{}"):
        self.id = tc_id
        self.type = "function"
        self.function = _Fn(name, args)


class _Resp:
    def __init__(self, content, tool_calls=None):
        msg = type("M", (), {"content": content, "tool_calls": tool_calls})()
        self.choices = [type("C", (), {"message": msg})()]
        self.usage = None


class _FakeCompletions:
    def __init__(self, responses):
        self.responses = responses
        self.calls = 0
        self.kwargs = []  # each create()'s kwargs, so tests can assert on tool_choice

    async def create(self, **kw):
        self.kwargs.append(kw)
        resp = self.responses[min(self.calls, len(self.responses) - 1)]
        self.calls += 1
        return resp


def _client_with(responses) -> tuple[OpenAIClient, _FakeCompletions]:
    client = OpenAIClient("m", "k")
    comp = _FakeCompletions(responses)
    client._client = type("Cl", (), {"chat": type("Ch", (), {"completions": comp})()})()
    return client, comp


async def test_converse_tool_choice_forces_first_step_only():
    # `tool_choice` forces the named tool on the first model call, then reverts to
    # free choice — so the loop can't get stuck re-forcing the same call.
    remember = Tool("remember", "d", {"type": "object", "properties": {}}, terminal=True)
    client, comp = _client_with([_Resp("", [_TC("1", "remember")]), _Resp("done")])

    async def execute(name, args):
        return "ok"

    await client.converse(
        "sys", [Msg("user", "hi")], tools=[remember], execute=execute, tool_choice="remember"
    )
    assert comp.calls == 2
    assert comp.kwargs[0]["tool_choice"] == {"type": "function", "function": {"name": "remember"}}
    assert "tool_choice" not in comp.kwargs[1]  # continuation chooses freely


class _ABlock:
    def __init__(self, type, text="", name="", id="", input=None):
        self.type = type
        self.text = text
        self.name = name
        self.id = id
        self.input = input or {}


class _AResp:
    def __init__(self, blocks):
        self.content = blocks
        self.usage = None


class _FakeMessages:
    def __init__(self, responses):
        self.responses = responses
        self.calls = 0
        self.kwargs = []

    async def create(self, **kw):
        self.kwargs.append(kw)
        resp = self.responses[min(self.calls, len(self.responses) - 1)]
        self.calls += 1
        return resp


def _anthropic_with(responses) -> tuple[AnthropicClient, _FakeMessages]:
    client = AnthropicClient("m", api_key="k")
    msgs = _FakeMessages(responses)
    client._client = type("Cl", (), {"messages": msgs})()
    return client, msgs


# --- provider drivers: one converse-loop policy, tested against BOTH adapters ------
# The loop's terminal/read/drop/token semantics live once in each adapter and must
# match. These drivers translate a provider-neutral scenario (reply text + tool
# calls, per step) into each SDK's response shape and read back the offered tools /
# usage, so the five loop tests below run identically against OpenAI and Anthropic.


class _OpenAIDriver:
    """Drives the OpenAI adapter with SDK-shaped completion stubs (see `_Resp`/`_TC`)."""

    client_with = staticmethod(_client_with)

    @staticmethod
    def resp(text, tool_calls=None, *, prompt_tokens=None):
        # tool_calls: list of (id, name). None text ⇒ the model wrote no reply this step.
        tcs = [_TC(tc_id, name) for tc_id, name in tool_calls] if tool_calls else None
        r = _Resp(text, tcs)
        if prompt_tokens is not None:
            r.usage = type("U", (), {"prompt_tokens": prompt_tokens, "completion_tokens": 5,
                                     "prompt_tokens_details": None})()
        return r

    @staticmethod
    def offered(kw):
        # The tool names in a create() kwargs dict — "OMITTED" when no tools key was sent.
        if "tools" not in kw:
            return "OMITTED"
        return [t["function"]["name"] for t in kw["tools"]]


class _AnthropicDriver:
    """Drives the Anthropic adapter with Messages-shaped stubs (see `_ABlock`/`_AResp`)."""

    client_with = staticmethod(_anthropic_with)

    @staticmethod
    def resp(text, tool_calls=None, *, prompt_tokens=None):
        blocks = []
        if text:  # None/"" ⇒ no text block (the model wrote no reply this step)
            blocks.append(_ABlock("text", text=text))
        for tc_id, name in tool_calls or []:
            blocks.append(_ABlock("tool_use", name=name, id=tc_id))
        r = _AResp(blocks)
        if prompt_tokens is not None:
            r.usage = type("U", (), {"input_tokens": prompt_tokens, "output_tokens": 5,
                                     "cache_read_input_tokens": 0,
                                     "cache_creation_input_tokens": 0})()
        return r

    @staticmethod
    def offered(kw):
        if "tools" not in kw:
            return "OMITTED"
        return [t["name"] for t in kw["tools"]]


_DRIVERS = [
    pytest.param(_OpenAIDriver, id="openai"),
    pytest.param(_AnthropicDriver, id="anthropic"),
]


@pytest.mark.parametrize("drv", _DRIVERS)
async def test_converse_terminal_tool_stops_after_one_call(drv):
    # Reply text + a terminal write tool in one step → run it, no extra call.
    remember = Tool("remember", "d", {"type": "object", "properties": {}}, terminal=True)
    client, fake = drv.client_with([drv.resp("reply text", [("1", "remember")])])
    ran = []

    async def execute(name, args):
        ran.append(name)
        return "ok"

    resp = await client.converse("sys", [Msg("user", "hi")], tools=[remember], execute=execute)
    assert resp.text == "reply text"
    assert ran == ["remember"]  # the write ran
    assert fake.calls == 1  # but no second round-trip to hear nothing back


async def test_anthropic_converse_tool_choice_forces_first_step_only():
    # Same loop-safe forcing as the OpenAI adapter, with Anthropic's payload shape —
    # the default brain's converse loop, previously exercised only via OpenAI.
    remember = Tool("remember", "d", {"type": "object", "properties": {}}, terminal=True)
    client, msgs = _anthropic_with([
        _AResp([_ABlock("tool_use", name="remember", id="t1")]),
        _AResp([_ABlock("text", text="done")]),
    ])

    async def execute(name, args):
        return "ok"

    await client.converse(
        "sys", [Msg("user", "hi")], tools=[remember], execute=execute, tool_choice="remember"
    )
    assert msgs.calls == 2
    assert msgs.kwargs[0]["tool_choice"] == {"type": "tool", "name": "remember"}
    assert "tool_choice" not in msgs.kwargs[1]  # continuation chooses freely


async def test_converse_no_tool_choice_by_default():
    # Default: nothing forced — the elective path capable brains use.
    lookup = Tool("lookup", "d", {"type": "object", "properties": {}})
    client, comp = _client_with([_Resp("hi")])

    async def execute(name, args):
        return "ok"

    await client.converse("sys", [Msg("user", "hi")], tools=[lookup], execute=execute)
    assert "tool_choice" not in comp.kwargs[0]


@pytest.mark.parametrize("drv", _DRIVERS)
async def test_converse_terminal_tool_dropped_after_use(drv):
    # If the model calls the write tool WITHOUT reply text, the loop continues
    # for the reply — but the terminal tool is dropped so it can't re-emit the
    # same write (the duplicate-remember bug). Reads stay offered.
    remember = Tool("remember", "d", {"type": "object", "properties": {}}, terminal=True)
    lookup = Tool("lookup", "d", {"type": "object", "properties": {}})
    client, fake = drv.client_with(
        [drv.resp(None, [("1", "remember")]), drv.resp("final reply", None)]
    )
    ran = []

    async def execute(name, args):
        ran.append(name)
        return "ok"

    resp = await client.converse(
        "sys", [Msg("user", "hi")], tools=[remember, lookup], execute=execute
    )
    assert resp.text == "final reply"
    assert ran == ["remember"]  # executed exactly once
    assert drv.offered(fake.kwargs[0]) == ["remember", "lookup"]  # both offered first step
    assert drv.offered(fake.kwargs[1]) == ["lookup"]  # remember withdrawn on the continuation


@pytest.mark.parametrize("drv", _DRIVERS)
async def test_converse_lone_terminal_tool_omits_empty_tools_on_continuation(drv):
    # The outreach/reflection shape: a SINGLE terminal tool, called with no reply
    # text. Dropping it empties specs — the continuation must be made WITHOUT a
    # tools kwarg (OpenAI/xAI 400 on `tools: []`; Anthropic keeps parity), not with
    # an empty array.
    decide = Tool("decide", "d", {"type": "object", "properties": {}}, terminal=True)
    client, fake = drv.client_with([drv.resp(None, [("1", "decide")]), drv.resp("done", None)])
    ran = []

    async def execute(name, args):
        ran.append(name)
        return "ok"

    resp = await client.converse("sys", [Msg("user", "hi")], tools=[decide], execute=execute)
    assert ran == ["decide"]  # the terminal tool ran (its decision was captured)
    assert resp.text == "done"  # the continuation COMPLETED instead of 400ing
    assert drv.offered(fake.kwargs[0]) == ["decide"]
    assert drv.offered(fake.kwargs[1]) == "OMITTED"  # no tools kwarg on the continuation, not []


@pytest.mark.parametrize("drv", _DRIVERS)
async def test_converse_reports_last_step_input_tokens_not_the_sum(drv):
    # Session sizing must track the last prompt's size, not the cross-step sum.
    lookup = Tool("lookup", "d", {"type": "object", "properties": {}})
    r1 = drv.resp(None, [("1", "lookup")], prompt_tokens=1000)
    r2 = drv.resp("answer", None, prompt_tokens=1500)
    client, _ = drv.client_with([r1, r2])

    async def execute(name, args):
        return "result"

    resp = await client.converse("sys", [Msg("user", "hi")], tools=[lookup], execute=execute)
    assert resp.input_tokens == 2500  # summed for billing
    assert resp.last_input_tokens == 1500  # final prompt only → what rotation uses


@pytest.mark.parametrize("drv", _DRIVERS)
async def test_converse_read_tool_loops(drv):
    # A non-terminal read tool: the model needs the result, so the loop runs again.
    lookup = Tool("lookup", "d", {"type": "object", "properties": {}})
    client, fake = drv.client_with(
        [drv.resp(None, [("1", "lookup")]), drv.resp("final answer", None)]
    )

    async def execute(name, args):
        return "result"

    resp = await client.converse("sys", [Msg("user", "hi")], tools=[lookup], execute=execute)
    assert resp.text == "final answer"
    assert fake.calls == 2


def test_anthropic_caches_stable_system_prefix():
    # A System renders as text blocks with a cache breakpoint on the stable
    # part; the volatile clock trails uncached.
    blocks = _system_param(System(stable="persona + core", volatile="it's 14:30"))
    assert blocks[0]["text"] == "persona + core"
    assert blocks[0]["cache_control"] == {"type": "ephemeral"}
    assert blocks[1]["text"] == "it's 14:30"
    assert "cache_control" not in blocks[1]
    # A plain string is passed through unchanged (no caching).
    assert _system_param("plain") == "plain"


def test_anthropic_skips_the_cache_write_when_a_system_opts_out():
    # System(cache=False) — for paths that can never re-read their own prefix within the
    # cache TTL (the outreach consult): no breakpoint, so no ~25%-premium cache write.
    blocks = _system_param(System(stable="persona + core", volatile="it's 14:30", cache=False))
    assert blocks[0]["text"] == "persona + core"
    assert "cache_control" not in blocks[0]


def test_openai_flattens_system_with_clock_last():
    text = _system_text(System(stable="persona", volatile="it's 14:30"))
    assert text == "persona\n\nit's 14:30"  # stable first, volatile (clock) last
    assert _system_text("plain") == "plain"


def test_openai_reads_cached_token_count():
    details = type("D", (), {"cached_tokens": 1280})()
    usage = type("U", (), {"prompt_tokens": 1500, "prompt_tokens_details": details})()
    assert _cached_tokens(usage) == 1280
    # Tolerates providers that don't report cache details.
    assert _cached_tokens(type("U", (), {"prompt_tokens": 10})()) == 0
    assert _cached_tokens(None) == 0


def test_build_none_provider_returns_none():
    assert build_client(_cfg(llm_provider="none")) is None


def test_build_unsupported_auth_raises(tmp_path, monkeypatch):
    monkeypatch.setenv("MOKO_HOME", str(tmp_path))  # isolate from any real tokens
    # Claude Code with no `claude` login present → a clear UnsupportedAuth.
    monkeypatch.setattr("moko.llm.claude_oauth.read_credentials", lambda: None)
    with pytest.raises(UnsupportedAuth):
        build_client(_cfg(llm_provider="anthropic", llm_auth_method="claude_code"))
    with pytest.raises(UnsupportedAuth):
        # xai oauth with no stored tokens → must prompt for login
        build_client(_cfg(llm_provider="xai", llm_auth_method="oauth"))


def test_build_missing_key_raises(tmp_path, monkeypatch):
    monkeypatch.setenv("MOKO_HOME", str(tmp_path))
    with pytest.raises(UnsupportedAuth):
        build_client(_cfg(llm_provider="anthropic", llm_auth_method="api_key", llm_model="x"))


def test_build_anthropic(tmp_path, monkeypatch):
    monkeypatch.setenv("MOKO_HOME", str(tmp_path))
    secrets.set_api_key("anthropic", "sk-test")
    client = build_client(
        _cfg(llm_provider="anthropic", llm_auth_method="api_key", llm_model="claude-opus-4-8")
    )
    assert client.model == "claude-opus-4-8"
    assert client.needs_scaffolding is False  # frontier brain: no scaffolding


def test_build_xai_sets_base_url(tmp_path, monkeypatch):
    monkeypatch.setenv("MOKO_HOME", str(tmp_path))
    secrets.set_api_key("xai", "sk-test")
    client = build_client(_cfg(llm_provider="xai", llm_auth_method="api_key", llm_model="grok-4.3"))
    assert client.model == "grok-4.3"
    assert client.base_url == "https://api.x.ai/v1"
    assert client.needs_scaffolding is True  # Grok: scaffold tool-use + voice


def test_build_env_auth(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-env")
    client = build_client(_cfg(llm_provider="openai", llm_auth_method="env", llm_model="gpt-x"))
    assert client.model == "gpt-x"
    assert client.needs_scaffolding is False  # frontier OpenAI: no scaffolding


def test_build_xai_oauth_needs_login(tmp_path, monkeypatch):
    monkeypatch.setenv("MOKO_HOME", str(tmp_path))
    with pytest.raises(UnsupportedAuth):
        build_client(_cfg(llm_provider="xai", llm_auth_method="oauth", llm_model="grok-4.3"))


def test_build_xai_oauth_with_tokens(tmp_path, monkeypatch):
    import datetime as dt

    from moko.clock import now_utc
    from moko.config import secrets

    monkeypatch.setenv("MOKO_HOME", str(tmp_path))
    secrets.set_oauth_tokens(
        "xai",
        {
            "access_token": "tok",
            "refresh_token": "r",
            "expires_at": (now_utc() + dt.timedelta(hours=1)).isoformat(),
            "token_endpoint": "https://t",
            "client_id": "c",
        },
    )
    client = build_client(_cfg(llm_provider="xai", llm_auth_method="oauth", llm_model="grok-4.3"))
    assert client.model == "grok-4.3"
    # The generic-plan OAuth token bills through the standard API host.
    assert client.base_url == "https://api.x.ai/v1"
