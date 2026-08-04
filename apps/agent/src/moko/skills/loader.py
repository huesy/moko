"""Skills — Moko's capabilities as editable markdown, not a fat system prompt.

A skill is a ``SKILL.md`` file with YAML frontmatter (``name``, ``description``)
and a markdown body of guidance. Core skills ship in ``builtin/``; the companion
can add or override them in ``~/.moko/skills/<name>/SKILL.md``. They're injected
into the stable, cached system-prefix, so the prompt stays small and the
behaviour stays tunable without code changes.

Deliberately minimal: discover, parse, inject (all of them, name-sorted so the
cached prefix stays byte-stable). Moko can author its own skills at runtime
(``tools/extend.py``); they land in ``~/.moko/skills`` and override a built-in
by name — except the core ability names, which ``author_skill`` refuses to
shadow (``builtin_skill_names``). Ranking and conditional gating are still M8 —
not built until they're needed.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

_BUILTIN = Path(__file__).parent / "builtin"


@dataclass(frozen=True)
class Skill:
    name: str
    description: str
    body: str


def _split_frontmatter(text: str) -> tuple[dict, str]:
    """Split a ``---``-fenced YAML frontmatter block from the markdown body.

    Lenient: no frontmatter, or malformed YAML, just yields an empty dict and
    the whole text as the body — a skill file is never allowed to raise."""
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            try:
                meta = yaml.safe_load(text[3:end])
            except yaml.YAMLError:
                meta = None
            if isinstance(meta, dict):
                return meta, text[end + 4 :]
    return {}, text


def _parse(skill_file: Path) -> Skill | None:
    try:
        text = skill_file.read_text(encoding="utf-8")
    except OSError:
        return None
    meta, body = _split_frontmatter(text)
    name = str(meta.get("name") or skill_file.parent.name).strip()
    body = body.strip()
    if not name or not body:
        return None
    return Skill(name=name, description=str(meta.get("description") or "").strip(), body=body)


def _discover(skills_dir: Path) -> dict[str, Skill]:
    """Parse every ``<skills_dir>/<name>/SKILL.md``. A missing dir yields {}."""
    found: dict[str, Skill] = {}
    if not skills_dir.is_dir():
        return found
    for sub in sorted(skills_dir.iterdir()):
        skill_file = sub / "SKILL.md"
        if sub.is_dir() and skill_file.is_file():
            skill = _parse(skill_file)
            if skill is not None:
                found[skill.name] = skill
    return found


def load_skills(user_dir: Path | None = None) -> list[Skill]:
    """Moko's built-in skills, with any user skills overriding/adding by name.

    Returned name-sorted so the rendered block is byte-stable across turns
    (the prompt-cache prefix must not wobble)."""
    skills = _discover(_BUILTIN)
    if user_dir is not None:
        skills.update(_discover(user_dir))
    return [skills[name] for name in sorted(skills)]


def builtin_skill_names() -> set[str]:
    """The names of the shipped core skills — the ones a user (or Moko itself)
    must not silently shadow. `author_skill` guards against overwriting these."""
    return set(_discover(_BUILTIN))


def render_skills(skills: list[Skill]) -> str:
    """Format skills as a markdown block for the system prompt (empty if none)."""
    if not skills:
        return ""
    blocks = ["# Your skills"]
    blocks += [f"## {s.name}\n\n{s.body}" for s in skills]
    return "\n\n".join(blocks)
