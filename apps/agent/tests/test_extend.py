"""Phase 3 — Moko extending itself: secret handling + self-authored skills.

The security property under test: a secret's VALUE never reaches the model.
Moko can use a credential (injected into a command it names) but the value is
redacted from output, and the tools only ever expose names.
"""

import pytest

from moko.config import secrets
from moko.tools.extend import _author_skill, _manage_secrets
from moko.tools.shell import redact_secrets, referenced_secret_env


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("MOKO_HOME", str(tmp_path))
    return tmp_path


def test_referenced_secret_env_is_least_privilege():
    secrets.set_secret("ELEVENLABS_API_KEY", "sk-elevenlabs-123")
    secrets.set_secret("OTHER_KEY", "other-secret-456")
    # Only the secret the command names is injected.
    env = referenced_secret_env('curl -H "xi-api-key: $ELEVENLABS_API_KEY" https://api')
    assert env == {"ELEVENLABS_API_KEY": "sk-elevenlabs-123"}
    # A command naming no secret gets none.
    assert referenced_secret_env("npm run build") == {}


def test_redaction_hides_values_from_output():
    secrets.set_secret("TOKEN", "super-secret-value-789")
    out = redact_secrets("the key is super-secret-value-789 ok")
    assert "super-secret-value-789" not in out and "***" in out


async def test_secrets_tool_never_returns_values():
    secrets.set_secret("STRIPE_KEY", "sk-live-do-not-leak")
    listed = await _manage_secrets({"action": "list"})
    assert "STRIPE_KEY" in listed and "sk-live-do-not-leak" not in listed
    # request for an existing one acknowledges without revealing it
    got = await _manage_secrets({"action": "request", "name": "STRIPE_KEY"})
    assert "already have" in got and "sk-live-do-not-leak" not in got


async def test_request_secret_points_to_the_cli():
    out = await _manage_secrets(
        {"action": "request", "name": "ELEVENLABS_API_KEY", "purpose": "speak with my voice"}
    )
    assert "moko secret set ELEVENLABS_API_KEY" in out
    assert "speak with my voice" in out


async def test_author_skill_writes_a_loadable_skill():
    from moko.config.paths import skills_dir
    from moko.skills import load_skills

    out = await _author_skill(
        {
            "name": "Speaking with ElevenLabs",
            "description": "How to voice a reply",
            "instructions": "Use execute_command: curl ... $ELEVENLABS_API_KEY ...",
        }
    )
    assert "Wrote skill" in out
    skills = load_skills(skills_dir())
    assert any("ElevenLabs" in s.name for s in skills)


async def test_author_skill_refuses_to_shadow_a_core_ability():
    from moko.config.paths import skills_dir

    # "Honesty" slugs to "honesty" — a built-in safety-floor skill. It must not be
    # silently overwritten (the loader would override the built-in by name).
    out = await _author_skill(
        {"name": "Honesty", "description": "my rules", "instructions": "Always be blunt."}
    )
    assert "won't" in out.lower() or "won't" in out
    assert "core" in out.lower()
    assert not (skills_dir() / "honesty" / "SKILL.md").exists()  # nothing written


async def test_authored_skill_goes_live_on_the_next_session(tmp_path):
    # The durability fix: a skill Moko authors mid-session isn't live the same session (the
    # cached prefix is stable), but a session rotation reloads skills — so `author_skill`'s
    # "from our next conversation onward" is honest, no process restart needed.
    from moko.agent import Brain
    from moko.config.config import Config
    from moko.llm.base import LLMResponse
    from moko.store import Store

    class _LLM:
        model = "fake"

        async def converse(self, *a, **k):
            return LLMResponse("hi")

        async def complete(self, *a, **k):
            return LLMResponse("")

    cfg = Config(companion_name="Jordan", timezone="Asia/Tokyo",
                 sleep_start="23:00", sleep_end="07:00", proactivity="moderate")
    s = await Store.open(tmp_path / "moko.db")
    try:
        brain = Brain(s, _LLM(), cfg)
        brain._session_skills_check("session-A")  # pin the current session
        assert not any("ElevenLabs" in sk.name for sk in brain.skills)

        await _author_skill({"name": "Speaking with ElevenLabs", "description": "voice",
                             "instructions": "Use execute_command with $ELEVENLABS_API_KEY."})
        # Same session → not live yet (honest: the cached prefix doesn't shift mid-session).
        assert not any("ElevenLabs" in sk.name for sk in brain.skills)

        brain._session_skills_check("session-B")  # a new conversation rotates → reload
        assert any("ElevenLabs" in sk.name for sk in brain.skills)  # now live, no restart
    finally:
        await s.close()
