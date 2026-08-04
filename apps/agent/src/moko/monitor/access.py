"""Exposure tiers for Moko's outward-facing surfaces.

One ladder of increasing reach, chosen in plain language by the companion;
this module turns that choice into the concrete mechanics — what address to
bind, whether auth is required, and (for the LAN tier) the firewall command
the user must run. The monitor is the first consumer; future web tools read
the same setting.

Designed for non-technical users: the *choice* is "who can reach this", not
"what IP/port". The awkward truth that shapes the ladder — opening a LAN port
needs root (Moko runs unprivileged), but Tailscale needs neither root nor a
firewall hole — lives here, not in the UI.
"""

from __future__ import annotations

import ipaddress
import secrets as _secrets
import shutil
import socket
import subprocess

from ..config import secrets
from .server import DEFAULT_PORT

# The ladder, lowest reach first.
TIERS = ("local", "tailscale", "network")
DEFAULT_TIER = "local"

_TOKEN_SECRET = "monitor_token"  # name under which the access token is stored

# (value, short label, plain-language meaning) — drives the config TUI.
TIER_CHOICES = [
    (
        "local",
        "Only this computer",
        "The safe default. The dashboard is reachable only from this machine.",
    ),
    (
        "tailscale",
        "My own devices (Tailscale)",
        "Your phone and laptops, anywhere, over your private Tailscale network. "
        "Encrypted, password-protected, never exposed to strangers. Recommended for remote access.",
    ),
    (
        "network",
        "My home network (Wi-Fi)",
        "Any device on your local network. Password-protected, but anyone on the same "
        "Wi-Fi can reach the login page — and it needs a one-time firewall command.",
    ),
]

TIER_LABELS = {value: label for value, label, _ in TIER_CHOICES}

MONITOR_PORT = DEFAULT_PORT  # single source of truth: monitor.server.DEFAULT_PORT


def requires_auth(tier: str) -> bool:
    """Anything beyond loopback must be password-protected — a no-auth feed of
    Moko's private thoughts is fine on this machine and nowhere else."""
    return tier != "local"


def _is_cgnat(addr: str) -> bool:
    """Tailscale hands out addresses from 100.64.0.0/10 (CGNAT space)."""
    try:
        return ipaddress.ip_address(addr) in ipaddress.ip_network("100.64.0.0/10")
    except ValueError:
        return False


def tailscale_ip() -> str | None:
    """This machine's Tailscale IPv4, or None if Tailscale isn't up here."""
    if shutil.which("tailscale"):
        try:
            r = subprocess.run(
                ["tailscale", "ip", "-4"], capture_output=True, text=True, timeout=4, check=False
            )
            line = r.stdout.strip().splitlines()
            if r.returncode == 0 and line and _is_cgnat(line[0].strip()):
                return line[0].strip()
        except (OSError, subprocess.SubprocessError):
            pass
    # Fallback: scan interfaces for a tailnet address (e.g. tailscale CLI absent).
    if shutil.which("ip"):
        try:
            r = subprocess.run(
                ["ip", "-o", "-4", "addr", "show"],
                capture_output=True, text=True, timeout=4, check=False,
            )
            for parts in (ln.split() for ln in r.stdout.splitlines()):
                for tok in parts:
                    if tok.count(".") == 3 and _is_cgnat(tok.split("/")[0]):
                        return tok.split("/")[0]
        except (OSError, subprocess.SubprocessError):
            pass
    return None


def resolve_host(tier: str) -> tuple[str, str | None]:
    """Map a tier to a bind address. Returns (host, problem):
    ``problem`` is a human message if the tier can't be honoured (e.g. Tailscale
    requested but not running) — the caller falls back to local and surfaces it."""
    if tier == "local":
        return "127.0.0.1", None
    if tier == "network":
        return "0.0.0.0", None  # noqa: S104 — intentional LAN exposure, firewalled by the user
    if tier == "tailscale":
        ip = tailscale_ip()
        if ip:
            return ip, None
        return "127.0.0.1", "Tailscale isn't running here, so the monitor stayed local."
    return "127.0.0.1", f"Unknown access tier {tier!r}; staying local."


def primary_ip() -> str:
    """This machine's primary LAN IPv4 (best effort), for showing a reachable
    URL. No packets are actually sent — the UDP connect just picks a route."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("8.8.8.8", 80))
        return sock.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        sock.close()


def reachable_url(tier: str, port: int = MONITOR_PORT) -> str:
    """The address to actually point a browser at for a given tier."""
    if tier == "tailscale":
        host = tailscale_ip() or "127.0.0.1"
    elif tier == "network":
        host = primary_ip()
    else:
        host = "127.0.0.1"
    return f"http://{host}:{port}"


def get_token() -> str | None:
    return secrets.get_secret(_TOKEN_SECRET)


def ensure_token() -> str:
    """The access token for non-local tiers — generated once, then reused."""
    existing = get_token()
    if existing:
        return existing
    token = _secrets.token_urlsafe(18)
    secrets.set_secret(_TOKEN_SECRET, token)
    return token


def rotate_token() -> str:
    """Mint a fresh token (invalidates devices that had the old one)."""
    token = _secrets.token_urlsafe(18)
    secrets.set_secret(_TOKEN_SECRET, token)
    return token


def lan_firewall_command(port: int = MONITOR_PORT) -> str:
    """The one command a user must run to open the LAN tier's port. Scoped to
    private ranges, not the whole internet."""
    return (
        f"sudo ufw allow from 192.168.0.0/16 to any port {port} proto tcp "
        f'comment "moko monitor (LAN)"'
    )
