"""The tool layer: web tools (faked at the HTTP seam) and the consent policy."""

import httpx
import pytest

from moko.tools import WEB_TOOLS
from moko.tools.consent import ALLOW, ASK, DENY, decide
from moko.tools.web import (
    UnsafeURL,
    _host_is_public,
    _require_safe,
    fetch_url,
    html_to_text,
    search_web,
)


# ── consent policy (the gate's brain) ──────────────────────────────────
def test_consent_defaults_safe_allow_sensitive_ask():
    assert decide("web", "safe") == ALLOW
    assert decide("system", "sensitive") == ASK


def test_consent_per_category_override_wins():
    assert decide("system", "sensitive", {"system": DENY}) == DENY  # never auto-run
    assert decide("file", "safe", {"file": ASK}) == ASK  # extra caution if asked
    assert decide("web", "sensitive", {"web": ALLOW}) == ALLOW  # trusted category


def test_web_tools_are_safe_and_categorized():
    assert WEB_TOOLS and all(t.risk == "safe" and t.category == "web" for t in WEB_TOOLS)


# ── web (HTTP faked via MockTransport) ─────────────────────────────────
def test_html_to_text_drops_scripts_and_tags():
    out = html_to_text("<p>Hi <b>there</b></p><script>evil()</script>")
    assert out == "Hi there"


async def test_fetch_url_returns_readable_text():
    def handler(_req):
        return httpx.Response(200, text="<html><body><h1>Title</h1><p>Body here</p></body></html>")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:  # literal public IP → host check is hermetic (no real DNS)
        text = await fetch_url("http://1.1.1.1/page", client=client)
        assert "Title" in text and "Body here" in text
    finally:
        await client.aclose()


# ── SSRF floor: web_fetch can't be steered into the local machine ──────────────
def test_host_check_rejects_local_and_metadata_addresses():
    assert _host_is_public("1.1.1.1") is True  # public
    assert _host_is_public("127.0.0.1") is False  # loopback
    assert _host_is_public("169.254.169.254") is False  # cloud metadata (link-local)
    assert _host_is_public("10.0.0.5") is False  # private
    assert _host_is_public("192.168.1.1") is False  # private
    assert _host_is_public("0.0.0.0") is False  # unspecified


async def test_require_safe_refuses_nonweb_and_local():
    with pytest.raises(UnsafeURL):
        await _require_safe("file:///etc/passwd")  # not http(s)
    with pytest.raises(UnsafeURL):
        await _require_safe("http://127.0.0.1:8080/admin")  # loopback service


async def test_fetch_refuses_redirect_into_localhost():
    # A public URL that 302s to loopback must be refused at the hop, not followed —
    # untrusted page content can't bounce web_fetch into an internal service.
    def handler(_req):
        return httpx.Response(302, headers={"location": "http://127.0.0.1/secret"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(UnsafeURL):
            await fetch_url("http://1.1.1.1/page", client=client)
    finally:
        await client.aclose()


async def test_search_web_parses_results():
    html = (
        '<a class="result__a" href="https://a.example">Alpha</a>'
        '<a class="result__a" href="https://b.example">Beta</a>'
    )
    transport = httpx.MockTransport(lambda _r: httpx.Response(200, text=html))
    client = httpx.AsyncClient(transport=transport)
    try:
        hits = await search_web("anything", client=client)
        assert hits == [("Alpha", "https://a.example"), ("Beta", "https://b.example")]
    finally:
        await client.aclose()
