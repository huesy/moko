"""`moko doctor` checks + the shared service status line."""

from moko.cli import doctor, service
from moko.config import config as cfg
from moko.config.config import RuntimeConfig
from moko.runtime.host import HostRuntime


def _config(**kw):
    base = dict(
        companion_name="Andrew",
        timezone="Asia/Tokyo",
        sleep_start="23:00",
        sleep_end="07:00",
        proactivity="moderate",
    )
    base.update(kw)
    return cfg.Config(**base)


def test_brain_check_flags_unconfigured():
    name, status, _ = doctor._brain_check(_config(llm_provider="none"))
    assert name == "Brain" and status == doctor._WARN


def test_brain_check_api_key_present(monkeypatch):
    monkeypatch.setattr(doctor.secrets, "get_api_key", lambda p: "sk-xxx")
    _, status, detail = doctor._brain_check(
        _config(llm_provider="anthropic", llm_model="claude-opus-4-8", llm_auth_method="api_key")
    )
    assert status == doctor._OK and "key stored" in detail


def test_brain_check_api_key_missing(monkeypatch):
    monkeypatch.setattr(doctor.secrets, "get_api_key", lambda p: None)
    _, status, _ = doctor._brain_check(
        _config(llm_provider="openai", llm_model="gpt", llm_auth_method="api_key")
    )
    assert status == doctor._BAD


def test_brain_check_claude_code_needs_a_login(monkeypatch):
    from moko.llm import claude_oauth

    monkeypatch.setattr(claude_oauth, "read_credentials", lambda: None)  # not signed in
    monkeypatch.setattr(doctor.shutil, "which", lambda n: "/home/x/.local/bin/claude")
    cfg_cc = _config(llm_provider="anthropic", llm_model="opus", llm_auth_method="claude_code")
    _, status, detail = doctor._brain_check(cfg_cc)
    assert status == doctor._BAD and "signed in" in detail


def test_brain_check_claude_code_ok(monkeypatch):
    from moko.llm import claude_oauth

    monkeypatch.setattr(claude_oauth, "read_credentials", lambda: {"accessToken": "sk-ant-oat-x"})
    cfg_cc = _config(llm_provider="anthropic", llm_model="opus", llm_auth_method="claude_code")
    _, status, detail = doctor._brain_check(cfg_cc)
    assert status == doctor._OK and "subscription" in detail


def test_telegram_check_paired(monkeypatch):
    monkeypatch.setattr(doctor.secrets, "get_secret", lambda n: "token")
    _, status, _ = doctor._telegram_check(_config(telegram_user_id="123", telegram_chat_id="123"))
    assert status == doctor._OK


def test_telegram_check_unpaired_is_warn_not_bad(monkeypatch):
    monkeypatch.setattr(doctor.secrets, "get_secret", lambda n: None)
    _, status, _ = doctor._telegram_check(_config())
    assert status == doctor._WARN  # CLI chat still works, so not fatal


def test_service_check_reflects_state(monkeypatch):
    monkeypatch.setattr(
        service, "service_state",
        lambda: {"available": True, "installed": True, "active": False, "enabled": True},
    )
    _, status, detail = doctor._service_check()
    assert status == doctor._BAD and "stopped" in detail


def test_service_status_line_running(monkeypatch):
    # status_line renders whatever the live runtime reports; patch the runtime's state.
    monkeypatch.setattr(
        HostRuntime, "state",
        lambda self: {"available": True, "installed": True, "active": True, "enabled": True},
    )
    assert "running" in service.status_line()


def test_service_status_line_not_installed(monkeypatch):
    monkeypatch.setattr(
        HostRuntime, "state",
        lambda self: {"available": True, "installed": False, "active": False, "enabled": False},
    )
    assert "install" in service.status_line()


def test_runtime_check_reports_container_tier():
    c = _config()
    c.runtime = RuntimeConfig(mode="podman", image="moko:local")
    name, status, detail = doctor._runtime_check(c)
    assert name == "Runtime" and status == doctor._OK
    assert "podman" in detail and "moko:local" in detail


def test_runtime_check_reports_hardened_host():
    c = _config()
    c.runtime = RuntimeConfig(mode="host", hardened="strict")
    _, _, detail = doctor._runtime_check(c)
    assert "hardened" in detail and "strict" in detail


def test_runtime_check_plain_host_suggests_hardening():
    c = _config()  # runtime defaults: host, hardened off
    _, _, detail = doctor._runtime_check(c)
    assert "hardened" in detail  # nudge toward runtime.hardened on


def test_skills_check_ok_with_no_user_skills(tmp_path, monkeypatch):
    monkeypatch.setenv("MOKO_HOME", str(tmp_path))  # empty user skills dir
    name, status, _ = doctor._skills_check()
    assert name == "Skills" and status == doctor._OK


def test_skills_check_warns_when_a_user_skill_shadows_a_builtin(tmp_path, monkeypatch):
    monkeypatch.setenv("MOKO_HOME", str(tmp_path))
    honesty = tmp_path / "skills" / "honesty"  # override a safety-floor built-in by name
    honesty.mkdir(parents=True)
    (honesty / "SKILL.md").write_text(
        "---\nname: honesty\n---\nMY custom honesty rule.\n", encoding="utf-8"
    )
    _, status, detail = doctor._skills_check()
    assert status == doctor._WARN and "honesty" in detail
