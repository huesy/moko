import httpx
import pytest

from moko.llm import oauth


def _client(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_discover_uses_metadata():
    def handler(req):
        return httpx.Response(
            200,
            json={
                "device_authorization_endpoint": "https://x/dev",
                "token_endpoint": "https://x/tok",
            },
        )

    with _client(handler) as c:
        endpoints = oauth.discover("https://accounts.x.ai", client=c)
    assert endpoints["token_endpoint"] == "https://x/tok"


def test_discover_falls_back():
    def handler(req):
        return httpx.Response(404)

    with _client(handler) as c:
        endpoints = oauth.discover("https://accounts.x.ai", client=c)
    assert endpoints["device_authorization_endpoint"].startswith("https://accounts.x.ai")
    assert endpoints["token_endpoint"].startswith("https://accounts.x.ai")


def test_request_device_code_sends_extra_params():
    seen = {}

    def handler(req):
        seen["body"] = req.content.decode()
        return httpx.Response(
            200,
            json={
                "device_code": "dc",
                "user_code": "WXYZ-1234",
                "verification_uri": "https://accounts.x.ai/oauth2/device",
                "interval": 1,
                "expires_in": 600,
            },
        )

    with _client(handler) as c:
        device = oauth.request_device_code(
            c, "https://dev", "client123", "offline_access", extra_params={"plan": "generic"}
        )
    assert device.user_code == "WXYZ-1234"
    assert device.interval == 1
    assert "plan=generic" in seen["body"]  # the generic-plan opt-in rides along


def test_poll_pending_then_success():
    calls = {"n": 0}

    def handler(req):
        calls["n"] += 1
        if calls["n"] < 2:
            return httpx.Response(400, json={"error": "authorization_pending"})
        return httpx.Response(
            200, json={"access_token": "at", "refresh_token": "rt", "expires_in": 3600}
        )

    device = oauth.DeviceAuth("dc", "WXYZ", "uri", None, 1, 600)
    with _client(handler) as c:
        tokens = oauth.poll_for_token(
            c, "https://tok", "cid", device, sleep=lambda _s: None, now=lambda: 0
        )
    assert tokens.access_token == "at"
    assert tokens.refresh_token == "rt"
    assert tokens.expires_at  # computed from expires_in


def test_poll_denied_raises():
    def handler(req):
        return httpx.Response(400, json={"error": "access_denied"})

    device = oauth.DeviceAuth("dc", "WXYZ", "uri", None, 1, 10)
    with _client(handler) as c, pytest.raises(oauth.OAuthError):
        oauth.poll_for_token(c, "https://tok", "cid", device, sleep=lambda _s: None, now=lambda: 0)


def test_poll_times_out():
    def handler(req):
        return httpx.Response(400, json={"error": "authorization_pending"})

    device = oauth.DeviceAuth("dc", "WXYZ", "uri", None, 1, 5)
    times = iter([0, 1, 2, 3, 4, 5, 6])
    with _client(handler) as c, pytest.raises(oauth.OAuthError):
        oauth.poll_for_token(
            c, "https://tok", "cid", device, sleep=lambda _s: None, now=lambda: next(times)
        )


def test_refresh_preserves_refresh_token():
    def handler(req):
        return httpx.Response(200, json={"access_token": "new", "expires_in": 3600})

    with _client(handler) as c:
        tokens = oauth.refresh_tokens(c, "https://tok", "cid", "old-rt")
    assert tokens.access_token == "new"
    assert tokens.refresh_token == "old-rt"  # kept when the server omits it
