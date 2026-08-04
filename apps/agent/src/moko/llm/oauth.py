"""OAuth 2.0 device-authorization flow (RFC 8628) for headless sign-in.

Built to the standards so it works without a localhost browser callback —
ideal for a server you SSH into. Endpoints are discovered (RFC 8414) with
sensible fallbacks; the client_id/scopes are supplied by the caller (e.g.
a Moko OAuth client registered at console.x.ai), never hard-coded.

Synchronous on purpose: `moko login` is a blocking CLI step, and token
refresh at startup is a quick one-shot. Tests drive it with an
httpx.MockTransport — no network.
"""

from __future__ import annotations

import datetime as dt
import time
from dataclasses import dataclass

import httpx

from ..clock import now_utc

DEVICE_GRANT = "urn:ietf:params:oauth:grant-type:device_code"


class OAuthError(Exception):
    """A device-flow failure (denied, expired, or a server error)."""


@dataclass
class DeviceAuth:
    device_code: str
    user_code: str
    verification_uri: str
    verification_uri_complete: str | None
    interval: int
    expires_in: int


@dataclass
class Tokens:
    access_token: str
    refresh_token: str | None
    expires_at: str  # UTC ISO-8601
    scope: str | None = None
    token_type: str = "Bearer"


def discover(issuer: str, *, client: httpx.Client) -> dict:
    """Return {device_authorization_endpoint, token_endpoint}.

    Uses OAuth Authorization Server Metadata when published, else falls
    back to conventional paths under the issuer.
    """
    base = issuer.rstrip("/")
    # Try OpenID Connect discovery first (xAI uses it), then plain OAuth metadata.
    for path in ("/.well-known/openid-configuration", "/.well-known/oauth-authorization-server"):
        try:
            resp = client.get(f"{base}{path}", timeout=10)
            if resp.status_code == 200:
                data = resp.json()
                dae = data.get("device_authorization_endpoint")
                tok = data.get("token_endpoint")
                if dae and tok:
                    return {"device_authorization_endpoint": dae, "token_endpoint": tok}
        except (httpx.HTTPError, ValueError):
            continue
    return {
        "device_authorization_endpoint": f"{base}/oauth2/device/code",
        "token_endpoint": f"{base}/oauth2/token",
    }


def request_device_code(
    client: httpx.Client,
    device_endpoint: str,
    client_id: str,
    scope: str,
    *,
    extra_params: dict[str, str] | None = None,
) -> DeviceAuth:
    data = {"client_id": client_id}
    if scope:
        data["scope"] = scope
    if extra_params:
        # xAI-specific opt-ins (e.g. plan=generic) ride alongside the
        # standard device-authorization request.
        data.update(extra_params)
    resp = client.post(device_endpoint, data=data, timeout=15)
    resp.raise_for_status()
    body = resp.json()
    return DeviceAuth(
        device_code=body["device_code"],
        user_code=body["user_code"],
        verification_uri=body.get("verification_uri") or body.get("verification_url") or "",
        verification_uri_complete=body.get("verification_uri_complete"),
        interval=int(body.get("interval", 5)),
        expires_in=int(body.get("expires_in", 900)),
    )


def _tokens_from(body: dict, *, keep_refresh: str | None = None) -> Tokens:
    expires_in = int(body.get("expires_in", 3600))
    expires_at = (now_utc() + dt.timedelta(seconds=expires_in)).isoformat()
    return Tokens(
        access_token=body["access_token"],
        refresh_token=body.get("refresh_token") or keep_refresh,
        expires_at=expires_at,
        scope=body.get("scope"),
        token_type=body.get("token_type", "Bearer"),
    )


def poll_for_token(
    client: httpx.Client,
    token_endpoint: str,
    client_id: str,
    device: DeviceAuth,
    *,
    sleep=time.sleep,
    now=time.monotonic,
) -> Tokens:
    """Poll until the user authorises, or raise OAuthError on denial/timeout."""
    interval = device.interval
    deadline = now() + device.expires_in
    while now() < deadline:
        sleep(interval)
        resp = client.post(
            token_endpoint,
            data={
                "grant_type": DEVICE_GRANT,
                "device_code": device.device_code,
                "client_id": client_id,
            },
        )
        if resp.status_code == 200:
            return _tokens_from(resp.json())
        try:
            error = (resp.json() or {}).get("error")
        except ValueError:
            error = None
        if error == "authorization_pending":
            continue
        if error == "slow_down":
            interval += 5
            continue
        raise OAuthError(error or f"token endpoint returned HTTP {resp.status_code}")
    raise OAuthError("timed out waiting for authorization")


def refresh_tokens(
    client: httpx.Client, token_endpoint: str, client_id: str, refresh_token: str
) -> Tokens:
    resp = client.post(
        token_endpoint,
        data={
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": client_id,
        },
    )
    resp.raise_for_status()
    return _tokens_from(resp.json(), keep_refresh=refresh_token)
