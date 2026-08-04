"""The monitor's HTTP server: status snapshot + live event stream + the SPA.

Bound to **127.0.0.1 only**. It streams Moko's private thoughts and the
companion's messages, so it never leaves the machine and is never exposed to
the network — the local dashboard runs on the same box as the service, by
design. It is a pure observer of :mod:`moko.events`: read-only, downstream of
every turn, unable to affect Moko's behaviour.
"""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import json
import os
import platform
from pathlib import Path

from aiohttp import web

import moko

from .. import events
from ..clock import now_utc, utc_isoformat
from ..config.config import Config
from ..log import get_logger
from ..store import COMPANION_WORLD, MOKO_WORLD, Store
from . import brainview

_log = get_logger("monitor")

# Typed app keys (aiohttp's recommended pattern over bare string keys).
STORE_KEY: web.AppKey[Store] = web.AppKey("store", Store)
CONFIG_KEY: web.AppKey[Config] = web.AppKey("config", Config)
SPA_KEY: web.AppKey[Path | None] = web.AppKey("spa_dir", object)  # Path or None
TOKEN_KEY: web.AppKey[str] = web.AppKey("token", str)  # "" when auth is off (local tier)

_COOKIE = "moko_monitor"  # holds the access token after a successful login
_COOKIE_MAX_AGE = 60 * 60 * 24 * 30  # 30 days

# Localhost by design — the monitor streams private thoughts and the
# companion's messages, with no auth. Override with MOKO_MONITOR_HOST only on a
# network you trust (e.g. "0.0.0.0" for LAN access, ideally firewalled to the
# local subnet; or a Tailscale IP to keep it on your tailnet).
DEFAULT_HOST = os.environ.get("MOKO_MONITOR_HOST", "127.0.0.1")
DEFAULT_PORT = int(os.environ.get("MOKO_MONITOR_PORT", "4319"))
_SSE_PING_SECONDS = 15.0  # comment-ping to keep an idle SSE connection alive


def _spa_dir() -> Path | None:
    """Where the built monitor SPA lives, or None if it hasn't been built.
    Checks (1) an explicit override, (2) assets packaged beside this module,
    (3) the dev build under apps/monitor/dist."""
    here = Path(__file__).resolve()
    candidates: list[Path] = []
    if env := os.environ.get("MOKO_MONITOR_DIST"):
        candidates.append(Path(env))
    candidates.append(here.parent / "static")  # packaged: assets beside this module
    # Dev: <repo>/apps/monitor/dist. This module is at
    # <repo>/apps/agent/src/moko/monitor/server.py, so the sibling app is up
    # past agent/ — but don't bank on the depth: find the `apps/` ancestor.
    for parent in here.parents:
        if parent.name == "apps":
            candidates.append(parent / "monitor" / "dist")
            break
    for c in candidates:
        if (c / "index.html").exists():
            return c
    return None


def _subscription_status() -> dict | None:
    """Local, no-network read of the Claude Code token (when that's the auth).
    Reports hours remaining; never refreshes — status must stay cheap."""
    from ..llm import claude_oauth

    creds = claude_oauth.read_credentials()
    if not creds:
        return {"signed_in": False}
    exp = creds.get("expiresAt")
    if not exp:
        return {"signed_in": True}
    hours = (exp / 1000 - now_utc().timestamp()) / 3600
    return {"signed_in": True, "hours_left": round(hours, 1)}


async def build_status(store: Store, config: Config) -> dict:
    """The status snapshot for the header panel. Cheap store reads + cached
    process facts; the one subprocess (systemctl) is tolerated for a poll."""
    from ..cli import service

    status: dict = {
        "moko_version": moko.__version__,
        "python": platform.python_version(),
        "companion": config.companion_name,
        "timezone": config.timezone,
        "now": utc_isoformat(),
        "llm": {
            "provider": config.llm_provider,
            "model": config.llm_model,
            "auth": config.llm_auth_method,
        },
        "observers": events.BUS.subscriber_count,
    }
    with contextlib.suppress(Exception):
        status["counts"] = await store.counts()
    with contextlib.suppress(Exception):
        # service_state() shells out to systemctl — a blocking subprocess. Run it
        # off the loop so a slow/hung systemctl can't stall the whole agent.
        status["service"] = await asyncio.to_thread(service.service_state)
    if config.llm_auth_method == "claude_code":
        with contextlib.suppress(Exception):
            status["subscription"] = _subscription_status()
    return status


async def _status_handler(request: web.Request) -> web.Response:
    store = request.app[STORE_KEY]
    config = request.app[CONFIG_KEY]
    return web.json_response(await build_status(store, config))


async def _rhythms_handler(request: web.Request) -> web.Response:
    """Autonomous-rhythm countdowns (settle, reflection, dream, outreach,
    research) — earliest-eligible times, computed by the agent's one source of
    truth in agent/rhythms.py so the bar can't drift from the real gating."""
    from dataclasses import asdict

    from ..agent import rhythms

    snap = await rhythms.snapshot(request.app[STORE_KEY], request.app[CONFIG_KEY])
    return web.json_response([asdict(r) for r in snap])


# ── brain browser: read-only views of the mind ──────────────────────────────

_MAX_PAGE = 200  # cap a single page so a crafted ?limit= can't ask for everything


def _page(request: web.Request) -> dict[str, int]:
    """Parse ?limit=&offset= for the paged brain lists; bad/oversized values fall
    back to the serializer's default page size, never error."""
    out: dict[str, int] = {}
    try:
        limit = int(request.query.get("limit", ""))
        if limit > 0:
            out["limit"] = min(limit, _MAX_PAGE)
    except (TypeError, ValueError):
        pass
    try:
        offset = int(request.query.get("offset", ""))
        if offset > 0:
            out["offset"] = offset
    except (TypeError, ValueError):
        pass
    return out


async def _brain_thoughts(request: web.Request) -> web.Response:
    return web.json_response(await brainview.thoughts(
        request.app[STORE_KEY], **_page(request), world_id=_world_param(request)))


async def _brain_dreams(request: web.Request) -> web.Response:
    return web.json_response(await brainview.dreams(request.app[STORE_KEY], **_page(request)))


async def _processes(request: web.Request) -> web.Response:
    from ..tools.process import SUPERVISOR

    return web.json_response(SUPERVISOR.list())


async def _brain_gaps(request: web.Request) -> web.Response:
    return web.json_response(await brainview.gaps(
        request.app[STORE_KEY], **_page(request), world_id=_world_param(request)))


async def _usage_handler(request: web.Request) -> web.Response:
    """Token Tracker: the per-call LLM usage rows (paged) + roll-ups all-time and over the
    last 24h / 7d, plus counts of media Moko has generated (not token-priced — shown as
    units). So the companion can see exactly where their tokens go and keep an eye on cost."""
    import datetime as dt

    store = request.app[STORE_KEY]
    now = now_utc()
    day = (now - dt.timedelta(hours=24)).isoformat()
    week = (now - dt.timedelta(days=7)).isoformat()
    return web.json_response({
        "rows": await store.usage.recent_llm_calls(**_page(request)),
        "all_time": await store.usage.llm_call_summary(),
        "window_24h": await store.usage.llm_call_summary(since=day),
        "window_7d": await store.usage.llm_call_summary(since=week),
        "media": await store.generated_media_counts(),
    })


async def _brain_outreach(request: web.Request) -> web.Response:
    return web.json_response(await brainview.outreach(request.app[STORE_KEY], **_page(request)))


async def _brain_commitments(request: web.Request) -> web.Response:
    return web.json_response(await brainview.commitments(request.app[STORE_KEY], **_page(request)))


async def _brain_messages(request: web.Request) -> web.Response:
    return web.json_response(await brainview.messages(request.app[STORE_KEY], **_page(request)))


def _world_param(request: web.Request) -> str | None:
    """Parse ?world=companion|moko into a world id; anything else (incl. 'all') → None."""
    w = request.query.get("world", "").strip().lower()
    if w == "companion":
        return COMPANION_WORLD
    if w == "moko":
        return MOKO_WORLD
    return None


async def _brain_subjects(request: web.Request) -> web.Response:
    return web.json_response(
        await brainview.subjects(
            request.app[STORE_KEY], **_page(request), world_id=_world_param(request)
        )
    )


async def _usage_detail(request: web.Request) -> web.Response:
    """One LLM call's full I/O — the exact system prompt, request, and reply + tokens."""
    detail = await request.app[STORE_KEY].usage.llm_call_detail(request.match_info["id"])
    if detail is None:
        return web.json_response({"error": "not found"}, status=404)
    return web.json_response(detail)


async def _media_list(request: web.Request) -> web.Response:
    """Metadata for the media gallery; the bytes come from `_media_blob` per item."""
    return web.json_response(await brainview.media(
        request.app[STORE_KEY], **_page(request), world_id=_world_param(request)))


def _build_thumbnail(src: str, dst: str) -> None:
    """Sync Pillow resize (runs in an executor, off the loop). Max 320px, JPEG."""
    from PIL import Image

    with Image.open(src) as im:
        im = im.convert("RGB")
        im.thumbnail((320, 320))
        im.save(dst, "JPEG", quality=80)


async def _thumbnail(path: Path, media_id: str):
    """A cached ≤320px JPEG thumbnail for an image, built off the loop. None on failure (the
    caller then serves the full file). Cached under ~/.moko/media/thumbs/<id>.jpg."""
    from ..config.paths import media_dir

    dst = media_dir() / "thumbs" / f"{media_id}.jpg"
    if dst.is_file():
        return dst
    try:
        dst.parent.mkdir(parents=True, exist_ok=True)
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, _build_thumbnail, str(path), str(dst))
    except Exception:
        get_logger("monitor").warning("thumbnail failed for %s", media_id, exc_info=True)
        return None
    return dst if dst.is_file() else None


async def _media_blob(request: web.Request) -> web.Response:
    """Serve one artifact's bytes by id — for the gallery's <img>/<audio>/<video>. The path
    is never taken from the request: we look the row up by id and read its STORED relative
    path, then confirm the resolved file still sits under ~/.moko/media/ (defence in depth
    against a path that somehow escaped the media root). Auth-gated like every route."""
    from ..config.paths import media_dir

    art = await request.app[STORE_KEY].media_by_id(request.match_info["id"])
    if art is None:
        return web.json_response({"error": "not found"}, status=404)
    root = media_dir().resolve()
    path = (root / art.path).resolve()
    if root != path and root not in path.parents:  # escaped the media root → refuse
        return web.json_response({"error": "forbidden"}, status=403)
    if not path.is_file():
        return web.json_response({"error": "not found"}, status=404)
    # Gallery grids ask for a thumbnail (?thumb=1): serve a small cached JPEG instead of the
    # full image, so 100+ portraits don't each ship ~1 MB. Falls through to the full file for
    # non-images or if thumbnailing fails.
    if request.query.get("thumb") and art.kind == "image":
        thumb = await _thumbnail(path, art.id)
        if thumb is not None:
            return web.FileResponse(thumb, headers={
                "Content-Type": "image/jpeg", "X-Content-Type-Options": "nosniff",
            })
    mime = art.mime or "application/octet-stream"
    # The mime can be attacker-influenced (an inbound file's declared type), so never let the
    # browser sniff, and only render images/audio/video inline — anything else downloads,
    # so a sent text/html can't execute as script in the monitor's origin.
    inline = mime.split("/", 1)[0] in ("image", "audio", "video")
    safe = (art.filename or art.id).replace('"', "").replace("\n", "").replace("\r", "")[:120]
    # FileResponse streams off-thread (sendfile) with Range support — so it never blocks the
    # agent's loop on a big file, and audio/video can seek. Pre-set Content-Type so FileResponse
    # doesn't guess from the content-addressed (extensionless) path.
    return web.FileResponse(path, headers={
        "Content-Type": mime,
        "X-Content-Type-Options": "nosniff",
        "Content-Disposition": f'{"inline" if inline else "attachment"}; filename="{safe}"',
    })


async def _brain_subject(request: web.Request) -> web.Response:
    detail = await brainview.subject_detail(request.app[STORE_KEY], request.match_info["id"])
    if detail is None:
        return web.json_response({"error": "not found"}, status=404)
    return web.json_response(detail)


async def _brain_thought(request: web.Request) -> web.Response:
    detail = await brainview.thought_detail(request.app[STORE_KEY], request.match_info["id"])
    if detail is None:
        return web.json_response({"error": "not found"}, status=404)
    return web.json_response(detail)


async def _brain_gap(request: web.Request) -> web.Response:
    detail = await brainview.gap_detail(request.app[STORE_KEY], request.match_info["id"])
    if detail is None:
        return web.json_response({"error": "not found"}, status=404)
    return web.json_response(detail)


async def _stream_handler(request: web.Request) -> web.StreamResponse:
    """Server-Sent Events: backfill the recent ring, then stream live. One
    aiohttp connection per browser tab; it's just another bus subscriber."""
    resp = web.StreamResponse(
        headers={
            "Content-Type": "text/event-stream",
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",  # disable any proxy buffering
        }
    )
    await resp.prepare(request)
    sub = events.subscribe(backfill=True)
    try:
        while True:
            try:
                event = await asyncio.wait_for(sub.queue.get(), timeout=_SSE_PING_SECONDS)
            except TimeoutError:
                await resp.write(b": ping\n\n")  # keep the connection warm
                continue
            payload = json.dumps(event, ensure_ascii=False, default=str)
            await resp.write(f"data: {payload}\n\n".encode())
    except (ConnectionResetError, asyncio.CancelledError):
        pass  # browser tab closed / server shutting down
    finally:
        sub.close()
    return resp


_UNBUILT_PAGE = (
    "<!doctype html><meta charset=utf-8><title>Moko monitor</title>"
    "<body style='font-family:sans-serif;padding:3rem;max-width:40rem'>"
    "<h1>Moko monitor 🌸</h1><p>The dashboard UI isn't built yet. "
    "From the repo root:</p><pre>pnpm --filter @moko/monitor build</pre>"
    "<p>The API is live in the meantime: "
    "<a href='/api/status'>/api/status</a> · "
    "<code>/api/stream</code> (SSE).</p>"
)


async def _spa_handler(request: web.Request) -> web.StreamResponse:
    """Serve the built SPA: a real file if it exists, else index.html (so
    client-side routes resolve). Falls back to a hint page if unbuilt."""
    spa = request.app[SPA_KEY]
    if spa is None:
        return web.Response(text=_UNBUILT_PAGE, content_type="text/html")
    rel = request.match_info.get("path", "").lstrip("/")
    if rel:
        target = (spa / rel).resolve()
        # Guard against path traversal — only serve within the SPA dir.
        if spa in target.parents and target.is_file():
            # Content-hashed build assets are safe to cache forever (the name
            # changes whenever the content does); everything else must revalidate.
            cache = (
                "public, max-age=31536000, immutable"
                if rel.startswith("assets/")
                else "no-cache"
            )
            return web.FileResponse(target, headers={"Cache-Control": cache})
    # index.html must revalidate every load. Otherwise a rebuilt SPA (new asset
    # hashes) stays invisible until a manual hard-refresh — which is exactly how a
    # finished change can look "broken" in the browser while the server's correct.
    return web.FileResponse(spa / "index.html", headers={"Cache-Control": "no-cache"})


def _login_html(*, error: bool = False) -> str:
    note = (
        "<p class='err'>That password didn't match. Try again.</p>"
        if error
        else "<p class='hint'>This Moko dashboard is password-protected.</p>"
    )
    return f"""<!doctype html><html><head><meta charset=utf-8>
<meta name=viewport content="width=device-width, initial-scale=1">
<title>Moko · sign in</title><link rel=icon type=image/png href=/favicon.png><style>
  body{{margin:0;height:100vh;display:grid;place-items:center;background:#221a26;
    color:#f4eaf2;font-family:system-ui,-apple-system,'Segoe UI',sans-serif}}
  .card{{background:#2c2230;border:1px solid #43354a;border-radius:22px;
    padding:34px 30px;width:min(360px,90vw);text-align:center;
    box-shadow:0 18px 50px -22px rgba(216,86,140,.5)}}
  h1{{margin:.2em 0;font-size:1.5rem}} .b{{color:#ff6fae}}
  .hint,.err{{font-size:.9rem;color:#a594a0;margin:.2em 0 1.2em}}
  .err{{color:#ff9a7b}}
  input{{width:100%;padding:12px 14px;border-radius:12px;border:1px solid #43354a;
    background:#372b3d;color:#f4eaf2;font-size:1rem;box-sizing:border-box}}
  button{{margin-top:12px;width:100%;padding:12px;border:0;border-radius:999px;
    background:#ff6fae;color:#fff;font-weight:700;font-size:1rem;cursor:pointer}}
</style></head><body><form class=card method=post action="/login">
  <h1>Moko <span class=b>monitor</span> 🌸</h1>{note}
  <input type=password name=token placeholder="Access password" autofocus
    autocomplete="current-password">
  <button type=submit>Sign in</button>
</form></body></html>"""


async def _login_get(request: web.Request) -> web.Response:
    return web.Response(text=_login_html(), content_type="text/html")


async def _login_post(request: web.Request) -> web.StreamResponse:
    token = request.app[TOKEN_KEY]
    data = await request.post()
    submitted = str(data.get("token", "")).strip()
    if token and hmac.compare_digest(submitted, token):
        resp = web.HTTPFound("/")  # raised, not returned (aiohttp's contract)
        resp.set_cookie(
            _COOKIE, token, httponly=True, samesite="Lax", max_age=_COOKIE_MAX_AGE
        )
        raise resp
    return web.Response(text=_login_html(error=True), content_type="text/html", status=401)


def _auth_middleware(token: str):
    """Gate every route except the login endpoints (and the app icon) behind a
    token cookie. API calls get a 401; page loads get the login form."""

    @web.middleware
    async def middleware(request: web.Request, handler):
        # The favicon isn't sensitive, and gating it hides the browser-tab icon on
        # the login screen — where browsers cache the failure and never retry. Always
        # serve it.
        if request.path in ("/login", "/favicon.png"):
            return await handler(request)
        cookie = request.cookies.get(_COOKIE, "")
        if cookie and hmac.compare_digest(cookie, token):
            return await handler(request)
        if request.path.startswith("/api/"):
            return web.json_response({"error": "unauthorized"}, status=401)
        return web.Response(text=_login_html(), content_type="text/html", status=401)

    return middleware


def build_app(store: Store, config: Config, *, token: str = "") -> web.Application:
    """Build the monitor app. A non-empty ``token`` turns on the auth gate
    (used for every tier above ``local``); empty means open (loopback only)."""
    middlewares = [_auth_middleware(token)] if token else []
    app = web.Application(middlewares=middlewares)
    app[STORE_KEY] = store
    app[CONFIG_KEY] = config
    app[SPA_KEY] = _spa_dir()
    app[TOKEN_KEY] = token
    app.router.add_get("/login", _login_get)
    app.router.add_post("/login", _login_post)
    app.router.add_get("/api/status", _status_handler)
    app.router.add_get("/api/rhythms", _rhythms_handler)
    app.router.add_get("/api/stream", _stream_handler)
    app.router.add_get("/api/brain/thoughts", _brain_thoughts)
    app.router.add_get("/api/brain/dreams", _brain_dreams)
    app.router.add_get("/api/processes", _processes)
    app.router.add_get("/api/brain/gaps", _brain_gaps)
    app.router.add_get("/api/usage", _usage_handler)
    app.router.add_get("/api/usage/{id}", _usage_detail)
    app.router.add_get("/api/media", _media_list)
    app.router.add_get("/api/media/blob/{id}", _media_blob)
    app.router.add_get("/api/brain/outreach", _brain_outreach)
    app.router.add_get("/api/brain/commitments", _brain_commitments)
    app.router.add_get("/api/brain/messages", _brain_messages)
    app.router.add_get("/api/brain/subjects", _brain_subjects)
    app.router.add_get("/api/brain/subject/{id}", _brain_subject)
    app.router.add_get("/api/brain/thought/{id}", _brain_thought)
    app.router.add_get("/api/brain/gap/{id}", _brain_gap)
    # Everything else is the SPA (registered last so /api wins). The catch-all
    # serves dist files (intro.png, /assets/*) and falls back to index.html.
    app.router.add_get("/{path:.*}", _spa_handler)
    return app


async def serve(
    store: Store,
    config: Config,
    *,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    token: str = "",
) -> web.AppRunner:
    """Start the monitor server on the current loop and return its runner.
    The caller keeps the runner and awaits ``runner.cleanup()`` at shutdown."""
    app = build_app(store, config, token=token)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, host, port)
    await site.start()
    built = app[SPA_KEY] is not None
    _log.info(
        "monitor up at http://%s:%d (spa=%s, auth=%s)",
        host, port, "yes" if built else "unbuilt", "on" if token else "off",
    )
    return runner
