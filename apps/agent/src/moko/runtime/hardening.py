"""Systemd sandbox hardening for the host tier (SANDBOX.md).

Directives folded into the ``[Service]`` block of Moko's unit when
``runtime.hardened`` is on/strict. They bound what a runaway or prompt-injected
``execute_command`` can reach — defense in depth *beneath* the consent gate,
never a replacement. Every directive degrades safely on an older systemd/kernel
(systemd ignores ones it doesn't know), so a hardened unit still boots; `moko
doctor` reports what actually took effect.

Levels:
  off    — nothing (today's plain unit).
  on     — protect the system, kernel, and clock; drop new privileges. ``$HOME``
           stays reachable so M7 (acting on the real machine) still works.
  strict — additionally hide ``$HOME`` behind a tmpfs, re-exposing only
           ``~/.moko`` and the companion's chosen ``runtime.mounts``.

Deliberately excluded: ``MemoryDenyWriteExecute`` (a known footgun for Python
extensions that build executable trampolines) — the risk/robustness trade isn't
worth it for a personal daemon. Kept conservative on purpose.
"""

from __future__ import annotations

# Applied for both `on` and `strict`. Conservative, widely-safe baseline.
_BASE = (
    "NoNewPrivileges=yes",
    "ProtectSystem=strict",  # /usr /boot /efi read-only; $HOME still writable
    "ProtectClock=yes",
    "ProtectKernelTunables=yes",
    "ProtectKernelModules=yes",
    "ProtectKernelLogs=yes",
    "ProtectControlGroups=yes",
    "ProtectHostname=yes",
    "RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6",  # unix socket + IPv4/6 egress only
    "RestrictNamespaces=yes",
    "RestrictRealtime=yes",
    "RestrictSUIDSGID=yes",
    "LockPersonality=yes",
    "SystemCallArchitectures=native",
    "SystemCallFilter=@system-service",
    "SystemCallErrorNumber=EPERM",
    "PrivateTmp=yes",
)


def hardening_block(level: str, rw_paths: list[str]) -> str:
    """The hardening lines to splice into ``[Service]`` (empty for level ``off``).

    ``rw_paths`` are the host paths Moko must still write under ``ProtectSystem``/
    ``ProtectHome`` — always ``~/.moko`` plus any companion-chosen mounts (M7 reach).
    """
    if level not in ("on", "strict"):
        return ""
    lines = list(_BASE)
    if level == "strict":
        lines.append("ProtectHome=tmpfs")  # hide $HOME; re-expose only rw_paths below
    # Under ProtectSystem=strict (and ProtectHome=tmpfs) these must be granted back.
    lines += [f"ReadWritePaths={p}" for p in rw_paths if p]
    body = "\n".join(lines)
    return f"# --- hardening (runtime.hardened={level}) — see SANDBOX.md ---\n{body}\n"
