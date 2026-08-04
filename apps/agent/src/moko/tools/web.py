"""Web tools — read the open web. Both are SAFE (read-only): fetch a page's
text, and a best-effort keyless search (DuckDuckGo HTML). Network calls are
bounded and degrade to a friendly message rather than raising."""

from __future__ import annotations

import asyncio
import ipaddress
import re
import socket
from urllib.parse import urljoin, urlparse

import httpx

from ..llm import Tool
from .base import SAFE, AgentTool

_UA = "Mozilla/5.0 (compatible; Moko/1.0; +https://github.com/huesy/moko)"
_MAX_CHARS = 6000  # cap a page's text so it can't blow up the context
_TIMEOUT = 20.0
_MAX_REDIRECTS = 5
_DROP = re.compile(r"<(script|style|noscript)[^>]*>.*?</\1>", re.I | re.S)
_TAGS = re.compile(r"<[^>]+>")
_WS = re.compile(r"\s+")


class UnsafeURL(ValueError):
    """A fetch target (or redirect hop) that points at a local/private address —
    refused so web_fetch can't be steered into Moko's own machine or a cloud
    metadata endpoint (SSRF). Untrusted page content drives these URLs, so the
    check is the floor, not a nicety."""


def html_to_text(html: str) -> str:
    """Crude readable-text extraction: drop script/style, strip tags, collapse
    whitespace. Good enough to feed a model; not a full reader."""
    return _WS.sub(" ", _TAGS.sub(" ", _DROP.sub(" ", html))).strip()


def _host_is_public(host: str) -> bool:
    """Resolve `host` and require every address to be a public one — reject
    loopback, link-local (incl. 169.254.169.254 cloud metadata), private,
    reserved, multicast, unspecified. Runs DNS, so call it off the loop."""
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError:
        return False
    if not infos:
        return False
    for info in infos:
        try:
            ip = ipaddress.ip_address(info[4][0])
        except ValueError:
            return False
        if (ip.is_loopback or ip.is_link_local or ip.is_private or ip.is_reserved
                or ip.is_multicast or ip.is_unspecified):
            return False
    return True


async def _require_safe(url: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise UnsafeURL(f"won't fetch a non-web address: {url!r}")
    if not await asyncio.to_thread(_host_is_public, parsed.hostname):
        raise UnsafeURL(f"won't fetch a local/private address: {parsed.hostname}")


async def fetch_url(url: str, *, client: httpx.AsyncClient) -> str:
    """Fetch a page's readable text. Redirects are followed manually so each hop is
    re-validated — a public URL can't 302 into localhost/metadata/private space."""
    current = url
    for _ in range(_MAX_REDIRECTS + 1):
        await _require_safe(current)
        r = await client.get(
            current, follow_redirects=False, headers={"User-Agent": _UA}, timeout=_TIMEOUT
        )
        if r.is_redirect and (loc := r.headers.get("location")):
            current = urljoin(current, loc)
            continue
        r.raise_for_status()
        return html_to_text(r.text)[:_MAX_CHARS]
    raise UnsafeURL("too many redirects")


async def search_web(
    query: str, *, client: httpx.AsyncClient, limit: int = 5
) -> list[tuple[str, str]]:
    r = await client.post(
        "https://html.duckduckgo.com/html/",
        data={"q": query},
        headers={"User-Agent": _UA},
        timeout=_TIMEOUT,
    )
    r.raise_for_status()
    hits: list[tuple[str, str]] = []
    for m in re.finditer(r'result__a[^>]*href="([^"]+)"[^>]*>(.*?)</a>', r.text, re.S):
        hits.append((html_to_text(m.group(2)), m.group(1)))
        if len(hits) >= limit:
            break
    return hits


async def _run_fetch(args: dict) -> str:
    url = str(args.get("url") or "").strip()
    if not url:
        return "No URL given."
    try:
        async with httpx.AsyncClient() as client:
            return await fetch_url(url, client=client)
    except Exception as exc:
        return f"Couldn't fetch that page ({exc})."


async def _run_search(args: dict) -> str:
    query = str(args.get("query") or "").strip()
    if not query:
        return "No query given."
    try:
        async with httpx.AsyncClient() as client:
            hits = await search_web(query, client=client)
    except Exception as exc:
        return f"Search failed ({exc})."
    return "\n".join(f"- {title}: {url}" for title, url in hits) if hits else "No results."


WEB_SEARCH = AgentTool(
    spec=Tool(
        name="web_search",
        description="Search the web for a query; returns the top result titles and URLs.",
        parameters={
            "type": "object",
            "properties": {"query": {"type": "string", "description": "what to search for"}},
            "required": ["query"],
        },
    ),
    category="web",
    risk=SAFE,
    run=_run_search,
)

WEB_FETCH = AgentTool(
    spec=Tool(
        name="web_fetch",
        description="Fetch the readable text of a web page by its URL.",
        parameters={
            "type": "object",
            "properties": {"url": {"type": "string", "description": "the page URL"}},
            "required": ["url"],
        },
    ),
    category="web",
    risk=SAFE,
    run=_run_fetch,
)

WEB_TOOLS = [WEB_SEARCH, WEB_FETCH]
