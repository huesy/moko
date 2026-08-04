"""`moko login` — connect or refresh a provider's sign-in.

Auth in Moko is **per provider, not per role**: the same xAI Grok subscription can
power the brain *and* image/video/voice, so login is about providers, not "the
brain". `moko login` (no args) shows every slot's auth status and lets you
connect/refresh whichever needs it; `moko login xai` targets one.

The only real device-flow login is xAI Grok (headless-friendly: prints a URL +
code, no localhost callback, so it works over SSH). Anthropic via "Claude Code on
this machine" needs no login here — it reuses this machine's Claude Code
credentials. API keys are stored with `moko setup`/`moko secret`, not here.

Setup and `moko config` call `ensure_xai_connected()` to log in *at the moment you
pick the Grok subscription*, so you rarely need this command directly.
"""

from __future__ import annotations

import datetime as dt
import os

import httpx
import questionary

from ..clock import now_utc
from ..config import config as cfg
from ..config import secrets
from ..config.config import MODALITIES, Config
from ..llm import oauth
from ..theme import QUESTIONARY_STYLE, console

ISSUER = os.environ.get("MOKO_XAI_ISSUER", "https://auth.x.ai")
SCOPE = os.environ.get(
    "MOKO_XAI_SCOPE", "openid profile email offline_access grok-cli:access api:access"
)

# xAI's SHARED public OAuth client for CLI agents (the `grok-cli:access` scope is
# the giveaway). With it baked in, `moko login` needs only the user's Grok
# credentials — no app registration. $MOKO_XAI_CLIENT_ID overrides if xAI rotates it.
XAI_SHARED_CLIENT_ID = "b1a00492-073a-47ea-816f-4c329264a828"

# `plan=generic` opts the grant into xAI's generic plan tier so the subscription
# bills through the standard api.x.ai endpoint (and so xAI accepts the flow from
# clients not on its loopback allowlist). referrer is honest attribution.
XAI_AUTH_EXTRA = {"plan": "generic", "referrer": "moko"}


# ── status ──────────────────────────────────────────────────────────────────
def xai_status() -> str:
    """'connected' | 'expired' | 'missing' for the stored Grok subscription. A token
    with a refresh token self-heals on expiry, so it reads as connected."""
    blob = secrets.get_oauth_tokens("xai")
    if not blob or not blob.get("access_token"):
        return "missing"
    if blob.get("refresh_token"):
        return "connected"
    exp = blob.get("expires_at")
    if exp:
        try:
            if now_utc() >= dt.datetime.fromisoformat(exp):
                return "expired"
        except ValueError:
            pass
    return "connected"


def _anthropic_status() -> str:
    """Whether this machine's Claude Code credentials are present (the brain's auth
    when provider=anthropic, method=claude_code) — Moko reuses them; no login here."""
    from ..llm import claude_oauth

    return "connected (machine)" if claude_oauth.read_credentials() else "not signed in"


def _provider_status(provider: str, auth: str) -> str:
    """A short, human status for a (provider, auth) pair, for the slot table."""
    if provider in ("", "none"):
        return "—"
    if auth == "claude_code" or (provider == "anthropic" and auth in ("", "claude_code")):
        return _anthropic_status()
    if provider == "xai" and auth != "api_key":
        return {"connected": "connected", "expired": "expired — reconnect",
                "missing": "not connected"}[xai_status()]
    if auth == "env":
        return "env var"
    # api_key (or unset on a keyed provider): is a key stored?
    return "key set" if secrets.get_api_key(provider) else "no key"


def _slots(config: Config) -> list[tuple[str, str, str]]:
    """(role, provider, auth) for the brain + every configured modality."""
    out = [("brain", config.llm_provider, config.llm_auth_method)]
    for cap in MODALITIES:
        choice = config.model_for(cap)
        if choice.configured:
            out.append((cap, choice.provider, choice.auth))
    return out


# ── connect (the xAI device flow) ─────────────────────────────────────────────
def connect_xai() -> bool:
    """Run xAI's OAuth device flow and persist the tokens. Returns True on success,
    False if the user cancels or the flow errors. Reused by setup/config (inline) and
    `moko login`."""
    client_id = os.environ.get("MOKO_XAI_CLIENT_ID") or XAI_SHARED_CLIENT_ID
    if not client_id:
        console.print(
            "[muted]I don't have xAI's shared OAuth client id baked in, so I can't "
            "complete the subscription login on my own. Paste it or set "
            "$MOKO_XAI_CLIENT_ID (or use an API key instead).[/muted]"
        )
        client_id = (questionary.text(
            "xAI OAuth client ID (blank to cancel):", style=QUESTIONARY_STYLE
        ).ask() or "").strip()
        if not client_id:
            return False
    try:
        with httpx.Client() as http:
            endpoints = oauth.discover(ISSUER, client=http)
            device = oauth.request_device_code(
                http, endpoints["device_authorization_endpoint"],
                client_id, SCOPE, extra_params=XAI_AUTH_EXTRA,
            )
            target = device.verification_uri_complete or device.verification_uri
            console.print(
                f"\n[accent]Let's connect your Grok subscription.[/accent]\n"
                f"  1. open [moko]{target}[/moko]\n"
                f"  2. enter the code [accent]{device.user_code}[/accent]\n"
                f"[muted]Waiting for you to approve…[/muted]\n"
            )
            tokens = oauth.poll_for_token(http, endpoints["token_endpoint"], client_id, device)
    except oauth.OAuthError as exc:
        console.print(f"[moko]Sign-in didn't complete: {exc}[/moko]")
        return False
    except httpx.HTTPError as exc:
        console.print(f"[moko]Couldn't reach xAI: {exc}[/moko]")
        return False

    secrets.set_oauth_tokens("xai", {
        "access_token": tokens.access_token,
        "refresh_token": tokens.refresh_token,
        "expires_at": tokens.expires_at,
        "token_endpoint": endpoints["token_endpoint"],
        "client_id": client_id,
        "scope": tokens.scope or SCOPE,
    })
    console.print("[moko]Connected! 🌸 Your Grok subscription is ready.[/moko]")
    return True


def _claude_login_container(config) -> int:
    """Sign into Claude Code *inside* Moko's container, so the token lands in the
    bind-mounted ~/.claude (surviving image rebuilds). Requires the container to be up."""
    import subprocess

    from ..runtime import get_runtime

    rt = get_runtime(config)
    if not rt.state().get("active"):
        console.print(
            "[moko]Start Moko's container first:[/moko] [accent]moko service start[/accent], "
            "then run [accent]moko login claude[/accent] again."
        )
        return 1
    console.print(
        "[moko]Signing into Claude Code inside Moko's container…[/moko] "
        "[muted]follow the prompts; the login is saved in the container's ~/.claude and "
        "persists across rebuilds.[/muted]"
    )
    # Interactive exec (a TTY): the user completes Claude Code's own sign-in flow.
    return subprocess.run(rt.exec_argv("claude"), check=False).returncode


def ensure_xai_connected(*, interactive: bool = True) -> bool:
    """True if the Grok subscription is connected, connecting it inline first if needed
    (when interactive). Called at selection time in setup/config so you authenticate
    right when you pick the subscription — no dangling 'now run moko login'."""
    if xai_status() == "connected":
        return True
    if not interactive:
        return False
    return connect_xai()


# ── the command ───────────────────────────────────────────────────────────────
def _print_status(config: Config) -> None:
    slots = _slots(config)
    width = max((len(r) for r, _, _ in slots), default=5)
    console.print("[muted]Your sign-ins:[/muted]")
    for role, provider, auth in slots:
        prov = "—" if provider in ("", "none") else provider
        status = _provider_status(provider, auth)
        console.print(f"  [accent]{role.ljust(width)}[/accent]  {prov:<10} {status}")


def run(args: list[str] | None = None) -> int:
    config = cfg.load()
    if config is None:
        console.print("We haven't met yet — run [accent]moko setup[/accent] first. 🌸")
        return 0

    target = (args[0].lower() if args else "")
    if target in ("anthropic", "claude", "claude_code"):
        # In a container tier Claude Code lives *inside* the container (its login is
        # persisted to the bind-mounted ~/.claude) — so sign in there, not on the host.
        if config.runtime.is_container:
            return _claude_login_container(config)
        console.print(
            f"[moko]Anthropic uses this machine's Claude Code sign-in[/moko] "
            f"({_anthropic_status()}) — nothing to log in here. "
            "Sign into Claude Code if it isn't connected."
        )
        return 0
    if target and target != "xai":
        console.print(
            f"[moko]I can only log in xAI Grok via OAuth[/moko] — '{target}' isn't that."
        )
        return 1
    if target == "xai":
        return 0 if connect_xai() else 1

    # No target: show the table, then offer to connect/refresh the device-flow provider.
    _print_status(config)
    uses_xai = any(p == "xai" and a != "api_key" for _, p, a in _slots(config))
    status = xai_status()
    if status == "connected" and uses_xai:
        again = questionary.confirm(
            "Grok is connected. Reconnect/refresh it anyway?", default=False,
            style=QUESTIONARY_STYLE,
        ).ask()
        if not again:
            return 0
        return 0 if connect_xai() else 1
    # Not connected (or connected but unused) — offer to connect Grok.
    prompt = ("Reconnect your Grok subscription?" if status == "expired"
              else "Connect your Grok subscription now?")
    if questionary.confirm(prompt, default=uses_xai, style=QUESTIONARY_STYLE).ask():
        return 0 if connect_xai() else 1
    return 0
