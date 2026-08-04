"""Shared xAI Grok OAuth resolution.

One place that turns the stored Grok *subscription* (the tokens `moko login`
writes) into a current bearer + a refresh hook — reused by the brain (chat, see
`_build_xai_oauth`) and the media clients (image gen). Mirrors Hermes' proven
approach: the OAuth access token is just a `Bearer` against `api.x.ai/v1`,
identical to an API key — so the same subscription powers chat *and* images,
with no separate key. The token is refreshed in place near expiry for a
long-running daemon.
"""

from __future__ import annotations

import asyncio
import datetime as dt
from collections.abc import Awaitable, Callable

import httpx

from ..clock import now_utc
from ..config import secrets
from . import oauth

XAI_BASE_URL = "https://api.x.ai/v1"

# One shared resolution per process. The Grok refresh token is single-use, so the
# brain and every media builder MUST share ONE closure + ONE rotating `state` — if
# each built its own, the first client's refresh would rotate the token out from
# under the others (invalid_grant, capabilities silently dropping off). The memo holds
# the live `state` dict; a lookup reuses the closure whenever the stored token matches
# `state`'s ORIGINAL or CURRENT (rotated) access token, so rotations don't fork it. A
# fresh `moko login` writes an unrelated token, which no longer matches → rebuild.
_MEMO: tuple[dict, str, tuple[str, Callable[[bool], Awaitable[str]]]] | None = None


def resolve_xai_oauth() -> tuple[str, Callable[[bool], Awaitable[str]]] | None:
    """The current Grok OAuth access token + an async `refresh(force) -> token` hook
    (refreshes near expiry and persists the rotation), or None if no subscription is
    connected. Memoized module-wide so the brain and all media builders share ONE
    closure over the single-use refresh token. Callers pass `refresh` to a client that
    calls it before each request."""
    global _MEMO
    blob = secrets.get_oauth_tokens("xai")
    if not blob or not blob.get("access_token"):
        _MEMO = None  # subscription disconnected — drop any stale closure
        return None

    stored = blob["access_token"]
    if _MEMO is not None:
        memo_state, memo_origin, memo_pair = _MEMO
        # Reuse when the stored token is the one this closure started from OR the one it
        # has since rotated to (persisted by its own refresh) — both are "still ours".
        if stored in (memo_origin, memo_state.get("access_token")):
            return memo_pair

    state = dict(blob)  # refreshed in place and persisted as it rotates
    lock = asyncio.Lock()  # single-flight: one exchange at a time across all sharers

    async def refresh(force: bool = False) -> str:
        async with lock:
            # Re-read the store before deciding: another sharer (or `moko login`) may
            # have rotated the token since we last looked — adopt it, don't re-exchange.
            latest = secrets.get_oauth_tokens("xai") or {}
            if latest.get("access_token"):
                state.update(latest)
            due = force
            expires_at = state.get("expires_at")
            if not due and expires_at:
                try:
                    due = now_utc() >= dt.datetime.fromisoformat(expires_at) - dt.timedelta(
                        seconds=120
                    )
                except ValueError:
                    due = False
            if due and state.get("refresh_token") and state.get("token_endpoint"):

                def _do():
                    with httpx.Client() as http:
                        return oauth.refresh_tokens(
                            http,
                            state["token_endpoint"],
                            state["client_id"],
                            state["refresh_token"],
                        )

                fresh = await asyncio.to_thread(_do)  # sync HTTP off the event loop
                state.update(
                    access_token=fresh.access_token,
                    refresh_token=fresh.refresh_token,
                    expires_at=fresh.expires_at,
                )
                secrets.set_oauth_tokens("xai", state)
            return state["access_token"]

    _MEMO = (state, stored, (state["access_token"], refresh))
    return _MEMO[2]


def _reset_memo() -> None:
    """Drop the shared closure. For tests that swap the underlying secrets store between
    cases (each `MOKO_HOME` is a distinct subscription) so a prior case's memo can't leak."""
    global _MEMO
    _MEMO = None
