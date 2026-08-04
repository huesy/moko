"""`moko update` — release-aware version check + install routing. Network/gh/uv calls
are exercised manually; here we prove the pure logic and the decision flow.
"""

from __future__ import annotations

from subprocess import CompletedProcess

from moko.cli import update


def test_is_newer_compares_versions():
    assert update.is_newer("v0.2.0", "0.1.0")
    assert update.is_newer("0.2.0", "0.1.0")
    assert not update.is_newer("v0.1.0", "0.1.0")
    assert not update.is_newer("v0.1.0", "0.2.0")
    # a real release beats the pre-release dev build of the same number
    assert update.is_newer("v0.1.0", "0.1.0.dev5")
    # unparseable tag → treat "different" as newer (better to offer than to hide)
    assert update.is_newer("weird-tag", "0.1.0")


def test_latest_release_extracts_tag_and_wheel(monkeypatch):
    monkeypatch.setattr(update, "_release_json", lambda: {
        "tag_name": "v0.2.0",
        "assets": [
            {"name": "notes.txt", "browser_download_url": "u1"},
            {"name": "moko-0.2.0-py3-none-any.whl", "browser_download_url": "https://x/moko.whl"},
        ],
    })
    assert update.latest_release() == ("v0.2.0", "https://x/moko.whl")


def test_latest_release_none_without_a_wheel(monkeypatch):
    monkeypatch.setattr(update, "_release_json", lambda: {"tag_name": "v0.2.0", "assets": []})
    assert update.latest_release() is None


def test_latest_release_none_when_unreachable(monkeypatch):
    monkeypatch.setattr(update, "_release_json", lambda: None)
    assert update.latest_release() is None


def test_run_routes_git_checkout_to_git_path(monkeypatch, tmp_path):
    monkeypatch.setattr(update, "_git_checkout_root", lambda: tmp_path)
    seen = {}

    def fake_git(root):
        seen["git"] = root
        return 0

    monkeypatch.setattr(update, "_update_from_git", fake_git)
    assert update.run() == 0
    assert seen["git"] == tmp_path


def test_run_routes_wheel_install_to_release_path(monkeypatch):
    monkeypatch.setattr(update, "_git_checkout_root", lambda: None)
    seen = {}

    def fake_release(*, check_only):
        seen["check"] = check_only
        return 0

    monkeypatch.setattr(update, "_update_from_release", fake_release)
    assert update.run(check_only=True) == 0
    assert seen["check"] is True


def test_release_reports_up_to_date(monkeypatch):
    monkeypatch.setattr(update, "latest_release", lambda: ("v0.0.1", "url"))
    monkeypatch.setattr(update.moko, "__version__", "0.0.1")
    downloaded = []
    monkeypatch.setattr(update, "_download_wheel", lambda *a: downloaded.append(a))
    assert update._update_from_release(check_only=False) == 0
    assert downloaded == []  # nothing installed when already current


def test_release_check_only_does_not_install(monkeypatch):
    monkeypatch.setattr(update, "latest_release", lambda: ("v9.9.9", "url"))
    monkeypatch.setattr(update.moko, "__version__", "0.0.1")
    downloaded = []
    monkeypatch.setattr(update, "_download_wheel", lambda *a: downloaded.append(a))
    assert update._update_from_release(check_only=True) == 0
    assert downloaded == []  # --check never installs


def test_release_installs_and_restarts_when_newer(monkeypatch):
    monkeypatch.setattr(update, "latest_release", lambda: ("v9.9.9", "https://x/moko.whl"))
    monkeypatch.setattr(update.moko, "__version__", "0.0.1")
    monkeypatch.setattr(update, "_download_wheel", lambda tag, url: "/tmp/moko.whl")
    state = {}

    def fake_run(argv, **k):
        state["install"] = argv
        return CompletedProcess(argv, 0)

    monkeypatch.setattr(update.subprocess, "run", fake_run)

    def fake_restart():
        state["restarted"] = True

    monkeypatch.setattr(update, "_restart_service", fake_restart)
    assert update._update_from_release(check_only=False) == 0
    assert "install" in state["install"] and state["install"][-1] == "/tmp/moko.whl"
    assert state.get("restarted") is True


def test_release_reports_when_unreachable(monkeypatch):
    monkeypatch.setattr(update, "latest_release", lambda: None)
    assert update._update_from_release(check_only=False) == 1  # honest failure, no crash
