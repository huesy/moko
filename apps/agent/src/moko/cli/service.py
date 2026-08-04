"""`moko service` — install and manage Moko's always-on background service.

A thin facade over the runtime abstraction (`moko.runtime`): it resolves the
configured tier (host / hardened host / container — see SANDBOX.md) and delegates
every verb to it. The module-level helpers stay put because the rest of the CLI
and the monitor read them (`service_state`, `status_line`, `restart_quiet`,
`install`); they now reflect whichever runtime is configured, not just systemd.
"""

from __future__ import annotations

from ..config import config as cfg
from ..runtime import Runtime, get_runtime


def _runtime() -> Runtime:
    """The Runtime for the configured tier (host when Moko isn't set up yet)."""
    return get_runtime(cfg.load())


def run(action: str) -> int:
    return _runtime().dispatch(action)


def install() -> int:
    return _runtime().install()


def service_state() -> dict:
    """Quiet structured read of the service — available/installed/active/enabled."""
    return _runtime().state()


def status_line() -> str:
    """One-line, marked-up service status for `moko status`."""
    return _runtime().status_line()


def restart_quiet() -> bool:
    """Restart with no console output (for the config TUI); True iff running after."""
    return _runtime().restart_quiet()
