"""The runtime abstraction — how Moko's always-on daemon is managed.

`HostRuntime` uses ``systemd --user``; `ContainerRuntime` uses docker/podman.
Which one is live is chosen from config (``runtime.mode``), never hard-coded —
see SANDBOX.md. `cli/service.py` is a thin facade over `get_runtime(config)`.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from ..theme import console

# The verbs `moko service <action>` accepts, mapped to methods by `dispatch`.
ACTIONS = ("install", "uninstall", "start", "stop", "restart", "status", "logs")


class Runtime(ABC):
    """One way of running Moko's daemon. Backends implement the lifecycle verbs and
    three quiet reads (`state`, `status_line`, `restart_quiet`) the rest of the CLI
    and the monitor consume."""

    @abstractmethod
    def install(self) -> int: ...

    @abstractmethod
    def uninstall(self) -> int: ...

    @abstractmethod
    def start(self) -> int: ...

    @abstractmethod
    def stop(self) -> int: ...

    @abstractmethod
    def restart(self) -> int: ...

    @abstractmethod
    def status(self) -> int: ...

    @abstractmethod
    def logs(self) -> int: ...

    @abstractmethod
    def state(self) -> dict:
        """Quiet structured read: keys available / installed / active / enabled."""

    @abstractmethod
    def status_line(self) -> str:
        """One-line, marked-up status for `moko status`."""

    @abstractmethod
    def restart_quiet(self) -> bool:
        """Restart with no console output (for the config TUI's full-screen frame);
        True iff Moko is running afterwards. Never raises."""

    def dispatch(self, action: str) -> int:
        """Map a `moko service <action>` word to its method."""
        table = {
            "install": self.install,
            "uninstall": self.uninstall,
            "start": self.start,
            "stop": self.stop,
            "restart": self.restart,
            "status": self.status,
            "logs": self.logs,
        }
        fn = table.get(action)
        if fn is None:
            console.print(f"[moko]I don't know how to '{action}' the service.[/moko]")
            return 2
        return fn()
