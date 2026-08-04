"""ContainerRuntime command construction + state parsing (SANDBOX.md). The engine
calls (docker/podman) are exercised manually — they need a real engine — so here we
prove the argv we hand it and how we read `inspect` back.
"""

from __future__ import annotations

from pathlib import Path
from subprocess import CompletedProcess

from moko.config.config import Config, RuntimeConfig
from moko.config.paths import moko_home
from moko.runtime import get_runtime
from moko.runtime.container import (
    CONTAINER_HOME,
    ContainerRuntime,
    _engine_install_cmd,
)


def _config(mode="podman", mounts=None, name="moko", image="moko:local", port=4319) -> Config:
    c = Config(
        companion_name="A", timezone="UTC", sleep_start="23:00", sleep_end="07:00",
        proactivity="moderate",
    )
    c.runtime = RuntimeConfig(
        mode=mode, image=image, container_name=name, monitor_port=port, mounts=mounts or [],
    )
    return c


def test_get_runtime_returns_container_for_container_modes():
    assert isinstance(get_runtime(_config("podman")), ContainerRuntime)
    assert isinstance(get_runtime(_config("docker")), ContainerRuntime)


def test_run_argv_mounts_home_at_identical_path_and_publishes_loopback():
    rt = ContainerRuntime(_config(mode="podman", mounts=["/home/x/proj"]))
    home = str(moko_home())
    argv = rt.run_argv()

    assert argv[0] == "podman"
    assert "run" in argv and "-d" in argv
    assert argv[-1] == "moko:local"  # image is last
    # named + boot-persist
    assert _adjacent(argv, "--name", "moko")
    assert _adjacent(argv, "--restart", "unless-stopped")
    # identical-path bind mount (keeps the consent gate's path-math honest) + MOKO_HOME
    assert f"{home}:{home}" in argv
    assert f"MOKO_HOME={home}" in argv
    # Claude creds volume, mounted where Path.home()/.claude resolves inside the image
    assert f"{home}/claude:{CONTAINER_HOME}/.claude" in argv
    # monitor: bound 0.0.0.0 inside, published to host loopback only
    assert "MOKO_MONITOR_HOST=0.0.0.0" in argv
    assert "127.0.0.1:4319:4319" in argv
    # the companion's chosen M7 reach
    assert "/home/x/proj:/home/x/proj" in argv


def test_docker_mode_uses_docker_binary():
    rt = ContainerRuntime(_config(mode="docker"))
    assert rt.run_argv()[0] == "docker"
    assert rt.exec_argv("claude")[0] == "docker"


def test_exec_argv_targets_the_container_interactively():
    rt = ContainerRuntime(_config(mode="podman", name="moko"))
    assert rt.exec_argv("claude") == ["podman", "exec", "-it", "moko", "claude"]
    assert rt.exec_argv("ls", interactive=False) == ["podman", "exec", "moko", "ls"]


def _fake_engine(*, running="true", policy="unless-stopped", exists=True):
    def _e(*args, capture=True):
        if "inspect" in args:
            fmt = args[args.index("-f") + 1]
            if not exists:
                return CompletedProcess(args, 1, "", "no such object")
            if "State.Running" in fmt:
                return CompletedProcess(args, 0, running + "\n", "")
            if "RestartPolicy" in fmt:
                return CompletedProcess(args, 0, policy + "\n", "")
            return CompletedProcess(args, 0, "abc123\n", "")  # .Id
        return CompletedProcess(args, 0, "", "")

    return _e


def test_state_running(monkeypatch):
    rt = ContainerRuntime(_config())
    monkeypatch.setattr(rt, "_available", lambda: True)
    monkeypatch.setattr(rt, "_engine", _fake_engine(running="true"))
    s = rt.state()
    assert s == {"available": True, "installed": True, "active": True, "enabled": True}
    assert "running" in rt.status_line()


def test_state_created_but_stopped(monkeypatch):
    rt = ContainerRuntime(_config())
    monkeypatch.setattr(rt, "_available", lambda: True)
    monkeypatch.setattr(rt, "_engine", _fake_engine(running="false", policy="no"))
    s = rt.state()
    assert s["installed"] is True and s["active"] is False and s["enabled"] is False
    assert "stopped" in rt.status_line()


def test_state_not_created(monkeypatch):
    rt = ContainerRuntime(_config())
    monkeypatch.setattr(rt, "_available", lambda: True)
    monkeypatch.setattr(rt, "_engine", _fake_engine(exists=False))
    assert rt.state()["installed"] is False


def test_state_engine_unavailable():
    # No docker/podman on PATH → everything false, no crash.
    rt = ContainerRuntime(_config(mode="docker"))
    # (test env has no docker) — _available() is genuinely False here.
    s = rt.state()
    assert s["available"] in (False, True)  # tolerate a CI box that has docker
    if not s["available"]:
        assert s == {"available": False, "installed": False, "active": False, "enabled": False}


def test_dispatch_maps_actions(monkeypatch):
    rt = ContainerRuntime(_config())
    calls = []
    for verb in ("install", "uninstall", "start", "stop", "restart", "status", "logs"):
        monkeypatch.setattr(rt, verb, lambda v=verb: calls.append(v) or 0)
    for verb in ("install", "uninstall", "start", "stop", "restart", "status", "logs"):
        assert rt.dispatch(verb) == 0
    assert calls == ["install", "uninstall", "start", "stop", "restart", "status", "logs"]
    assert rt.dispatch("nonsense") == 2  # unknown verb


def _adjacent(seq, a, b) -> bool:
    """True if `a` is immediately followed by `b` somewhere in `seq`."""
    return any(seq[i] == a and seq[i + 1] == b for i in range(len(seq) - 1))


# ── auto-install: build context, engine detection, and the install flow ──────
def test_build_argv_uses_local_checkout_when_present(monkeypatch):
    rt = ContainerRuntime(_config(mode="podman"))
    monkeypatch.setattr("moko.runtime.container._local_context",
                        lambda: Path("/repo/apps/agent"))
    assert rt.build_argv() == ["podman", "build", "-t", "moko:local", "/repo/apps/agent"]


def test_build_argv_falls_back_to_git(monkeypatch):
    rt = ContainerRuntime(_config(mode="docker"))
    monkeypatch.setattr("moko.runtime.container._local_context", lambda: None)
    monkeypatch.delenv("MOKO_REPO", raising=False)
    monkeypatch.delenv("MOKO_REF", raising=False)
    monkeypatch.delenv("MOKO_VERSION", raising=False)
    argv = rt.build_argv()
    assert argv[:4] == ["docker", "build", "-t", "moko:local"]
    assert argv[4].endswith("#main:apps/agent") and "huesy/moko" in argv[4]


def test_build_argv_git_respects_repo_env(monkeypatch):
    rt = ContainerRuntime(_config(mode="podman"))
    monkeypatch.setattr("moko.runtime.container._local_context", lambda: None)
    monkeypatch.setenv("MOKO_REPO", "https://example.com/x.git")
    monkeypatch.setenv("MOKO_REF", "v2")
    assert rt.build_argv()[-1] == "https://example.com/x.git#v2:apps/agent"


def test_engine_install_cmd_detects_package_manager(monkeypatch):
    monkeypatch.setattr("moko.runtime.container.shutil.which",
                        lambda t: "/usr/bin/apt-get" if t == "apt-get" else None)
    assert _engine_install_cmd("podman") == ["sudo", "apt-get", "install", "-y", "podman"]


def test_engine_install_cmd_none_when_no_known_manager(monkeypatch):
    monkeypatch.setattr("moko.runtime.container.shutil.which", lambda t: None)
    assert _engine_install_cmd("docker") is None


def test_install_auto_builds_then_launches(monkeypatch):
    rt = ContainerRuntime(_config(mode="podman"))
    events: list[str] = []
    monkeypatch.setattr(rt, "_ensure_engine", lambda: True)
    monkeypatch.setattr(rt, "_image_present", lambda: False)
    monkeypatch.setattr(rt, "_build_image", lambda: events.append("build") or True)
    monkeypatch.setattr(rt, "_boot_persist", lambda: events.append("boot"))
    monkeypatch.setattr(
        rt, "_engine",
        lambda *a, capture=True: events.append(a[1]) or CompletedProcess(a, 0, "", ""),
    )
    assert rt.install() == 0
    assert events == ["build", "rm", "run", "boot"]  # build → replace stale → run → boot-persist


def test_install_skips_build_when_image_present(monkeypatch):
    rt = ContainerRuntime(_config(mode="podman"))
    built: list[bool] = []
    monkeypatch.setattr(rt, "_ensure_engine", lambda: True)
    monkeypatch.setattr(rt, "_image_present", lambda: True)
    monkeypatch.setattr(rt, "_build_image", lambda: built.append(True) or True)
    monkeypatch.setattr(rt, "_boot_persist", lambda: None)
    monkeypatch.setattr(rt, "_engine", lambda *a, capture=True: CompletedProcess(a, 0, "", ""))
    assert rt.install() == 0
    assert built == []  # no rebuild when the image already exists


def test_install_aborts_when_engine_unavailable(monkeypatch):
    rt = ContainerRuntime(_config(mode="podman"))
    ran: list = []
    monkeypatch.setattr(rt, "_ensure_engine", lambda: False)
    monkeypatch.setattr(
        rt, "_engine", lambda *a, capture=True: ran.append(a) or CompletedProcess(a, 0, "", ""))
    assert rt.install() == 1
    assert ran == []  # nothing runs (no build, no launch) if the engine isn't ready
