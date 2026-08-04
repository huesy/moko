"""Runtime tiers — where and how contained Moko's daemon runs (SANDBOX.md).

`get_runtime(config)` resolves the configured tier to a concrete `Runtime`:
host (the default; `systemd --user`, optionally hardened) or a container
(docker/podman). The CLI's lifecycle commands (`service`, and the monitor/status
reads) go through this, so the tier is one config choice — never hard-coded.
"""

from __future__ import annotations

from .base import Runtime
from .host import HostRuntime


def get_runtime(config=None) -> Runtime:
    """The Runtime for the configured tier. No config (pre-setup) → host."""
    rt = getattr(config, "runtime", None) if config is not None else None
    mode = getattr(rt, "mode", "host") if rt is not None else "host"
    if mode in ("docker", "podman"):
        from .container import ContainerRuntime

        return ContainerRuntime(config)
    return HostRuntime(config)


__all__ = ["Runtime", "HostRuntime", "get_runtime"]
