"""The OAuth-refresh seam on the OpenAI/xAI client: a long-running daemon's
token must refresh before it expires and recover from an auth error, rather than
403-ing until restart."""

import types

import httpx
import openai
import pytest

from moko.llm.base import Msg
from moko.llm.openai_client import OpenAIClient


def _resp():
    msg = types.SimpleNamespace(content="ok", tool_calls=None)
    usage = types.SimpleNamespace(prompt_tokens=1, completion_tokens=1, prompt_tokens_details=None)
    return types.SimpleNamespace(choices=[types.SimpleNamespace(message=msg)], usage=usage)


class _Completions:
    def __init__(self, fail_first):
        self.fail_first = fail_first
        self.calls = 0

    async def create(self, **kw):
        self.calls += 1
        if self.fail_first and self.calls == 1:
            raise openai.PermissionDeniedError(
                "bad token",
                response=httpx.Response(403, request=httpx.Request("POST", "https://api.x.ai")),
                body=None,
            )
        return _resp()


def _fake_client(completions):
    return types.SimpleNamespace(
        chat=types.SimpleNamespace(completions=completions), api_key="old"
    )


async def test_proactive_refresh_updates_the_bearer():
    async def refresh(force):
        return "fresh"

    client = OpenAIClient("m", "init", refresh=refresh)
    comp = _Completions(fail_first=False)
    client._client = _fake_client(comp)
    await client.complete("sys", [Msg("user", "hi")])
    assert comp.calls == 1
    assert client._client.api_key == "fresh"  # refreshed before the call


async def test_reactive_retry_once_on_auth_error():
    forced = []

    async def refresh(force):
        forced.append(force)
        return "forced" if force else "proactive"

    client = OpenAIClient("m", "init", refresh=refresh)
    comp = _Completions(fail_first=True)
    client._client = _fake_client(comp)
    await client.complete("sys", [Msg("user", "hi")])
    assert comp.calls == 2  # 403 → force-refresh → retry succeeds
    assert True in forced and client._client.api_key == "forced"


async def test_static_key_propagates_auth_error():
    client = OpenAIClient("m", "key")  # no refresh hook
    client._client = _fake_client(_Completions(fail_first=True))
    with pytest.raises(openai.PermissionDeniedError):
        await client.complete("sys", [Msg("user", "hi")])
