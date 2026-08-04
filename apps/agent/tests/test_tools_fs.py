"""Filesystem + system tools: read scoping, write, and shell exec."""

from moko.tools.consent import ALLOW, ASK, DENY
from moko.tools.files import FILE_TOOLS, _classify_write, _read_file, _resolve, _write_file
from moko.tools.shell import workspace
from moko.tools.system import _execute_command


def test_write_in_workspace_is_free_outside_is_gated():
    # The feed bug: building a site in ~/.moko/work prompted on every write_file.
    # In Moko's own work folder, writes run free; elsewhere they ask; secrets deny.
    assert _classify_write({"path": f"{workspace()}/site/index.html"}).verdict == ALLOW
    assert _classify_write({"path": "~/Documents/notes.txt"}).verdict == ASK
    assert _classify_write({"path": "~/.ssh/id_rsa"}).verdict == DENY


def test_read_refuses_sensitive_paths():
    assert _resolve("~/.ssh/id_rsa") is None  # blocked
    assert _resolve("/home/x/.moko/secrets.yaml") is None
    assert _resolve("/tmp/notes.txt") is not None  # ordinary path is fine


def test_file_tools_are_just_read_and_write():
    # Lean by design: listing/stat/delete go through the shell (one resource
    # policy), so the only dedicated file tools are the two that beat the shell.
    names = {t.spec.name for t in FILE_TOOLS}
    assert names == {"read_file", "write_file"}
    risks = {t.spec.name: t.risk for t in FILE_TOOLS}
    assert risks["read_file"] == "safe" and risks["write_file"] == "sensitive"


async def test_write_then_read_roundtrip(tmp_path):
    path = str(tmp_path / "hello.txt")
    assert "Wrote" in await _write_file({"path": path, "content": "hi there"})
    assert await _read_file({"path": path}) == "hi there"


async def test_read_blocked_path_is_refused(tmp_path):
    secret = tmp_path / "id_rsa"
    secret.write_text("PRIVATE KEY")
    out = await _read_file({"path": str(secret)})
    assert "not allowed" in out.lower()  # the denylist caught it


async def test_execute_command_captures_output():
    out = await _execute_command({"command": "echo moko-lives"})
    assert "moko-lives" in out and "exit 0" in out


def test_relative_write_anchors_to_workspace_not_cwd():
    # The bug: _classify_write said "in your workspace" (ALLOW) for a relative path,
    # but _write_file resolved it against the daemon's CWD — a silent, ungated
    # overwrite of whatever file sat there. Now classify and resolve agree: a
    # relative path is the workspace.
    ws = workspace().resolve()
    assert _classify_write({"path": "notes.txt"}).verdict == ALLOW
    target = _resolve("notes.txt")
    assert target is not None and ws in target.parents


async def test_relative_write_lands_in_the_workspace():
    out = await _write_file({"path": "probe-moko.txt", "content": "hi"})
    assert "Wrote" in out
    landed = workspace() / "probe-moko.txt"
    assert landed.read_text() == "hi"  # in the workspace, not the process CWD
    landed.unlink()


def test_relative_traversal_out_of_workspace_is_gated():
    # `../` escapes must gate (not free), and classify + resolve must agree on the
    # target so the gate covers where the write actually lands.
    escape = "../../../../../../../../etc/passwd"
    assert _classify_write({"path": escape}).verdict == ASK
    target = _resolve(escape)
    assert target is not None and workspace().resolve() not in target.parents


def test_dollar_home_resolves_to_real_home_not_a_literal_dir():
    # Boundary drift: `_classify_write` expands $HOME (via in_workspace) and gates
    # `$HOME/evil` as outside-workspace (ASK), but `_resolve` used to only expanduser
    # — so on approval it wrote a *literal* `$HOME/` dir INSIDE the workspace. Now
    # both expand $HOME to the real home, so classify and resolve agree on the target.
    from pathlib import Path

    home = Path.home()
    assert _classify_write({"path": "$HOME/evil"}).verdict == ASK
    target = _resolve("$HOME/evil")
    # real home, not a literal "$HOME" dir inside the workspace
    assert target == home / "evil"
    assert target is not None and workspace().resolve() not in target.parents
    assert _resolve("${HOME}/evil") == home / "evil"  # brace form too
