"""Tools for Moko to extend itself: acquire credentials and author its own
skills. The generic mechanism behind "set me up with service X and start using
it" — nothing service-specific lives here or in the core; the skill Moko writes
lands in ~/.moko/skills, learned at runtime.

Security stance: a secret's VALUE never flows through here. Moko can ask for a
credential by name and learn whether one exists, but the companion provides the
value out-of-band (`moko secret set NAME`, hidden input), and it's only ever read
by the shell at execution time (injected into the env of a command that names it,
redacted from output). The model never sees it.
"""

from __future__ import annotations

import re

import yaml

from ..config.paths import skills_dir
from ..config.secrets import get_secret, secret_names
from ..llm import Tool
from ..skills.loader import builtin_skill_names
from .base import SAFE, SENSITIVE, AgentTool


# ── secrets: request a credential / list what's stored (never values) ─────────
async def _manage_secrets(args: dict) -> str:
    action = str(args.get("action") or "list").strip().lower()
    if action == "list":
        names = secret_names()
        return "Stored credentials: " + (", ".join(names) if names else "(none yet)")
    if action == "request":
        name = str(args.get("name") or "").strip()
        if not name:
            return "Which credential? Give the name you'd store it under (e.g. ELEVENLABS_API_KEY)."
        purpose = str(args.get("purpose") or "").strip()
        if get_secret(name) is not None:
            return f"You already have '{name}' stored — I can use it. (I never see the value.)"
        why = f" so I can {purpose}" if purpose else ""
        return (
            f"I need a credential called '{name}'{why}. To give it to me, run this in a "
            f"terminal:\n  moko secret set {name}\nIt's entered privately, stays on your "
            f"machine, and I only ever use it inside a command — I never see the value itself. "
            f"Tell me once it's set and I'll continue."
        )
    return f"Unknown action '{action}'. Use list or request."


SECRETS = AgentTool(
    spec=Tool(
        name="secrets",
        description=(
            "Acquire or check API credentials. action='list' shows which credentials "
            "are stored (names only); 'request' (give name, optional purpose) tells the "
            "companion how to provide one. You never see secret values — once stored, "
            "reference a credential in a shell command as $NAME and it's injected for you."
        ),
        parameters={
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["list", "request"]},
                "name": {"type": "string", "description": "credential name, e.g. SERVICE_API_KEY"},
                "purpose": {"type": "string", "description": "what you need it for"},
            },
            "required": ["action"],
        },
    ),
    category="system",
    risk=SAFE,
    run=_manage_secrets,
)


# ── author_skill: Moko writes itself a how-to (gated; durable new behavior) ────
def _slug(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", name.strip().lower()).strip("-")
    return s[:48] or "skill"


async def _author_skill(args: dict) -> str:
    name = str(args.get("name") or "").strip()
    description = str(args.get("description") or "").strip()
    instructions = str(args.get("instructions") or "").strip()
    if not name or not instructions:
        return "A skill needs a name and instructions."
    slug = _slug(name)
    # Never let a self-authored skill silently shadow a core ability — the honesty
    # boundary, consent gating, and the like ship as builtin skills, and a same-named
    # user skill would override them at load time. Refuse honestly instead.
    if slug.lower() in {n.lower() for n in builtin_skill_names()}:
        return (
            f"I won't do that — '{name}' would overwrite one of my core abilities, and those "
            f"aren't mine to quietly rewrite. Pick a different name and I'll happily write it."
        )
    skill_dir = skills_dir() / slug
    skill_dir.mkdir(parents=True, exist_ok=True)
    # Emit the frontmatter via YAML so a model-supplied name/description with a colon, '#',
    # or newline is quoted/escaped correctly (an f-string would produce invalid YAML that the
    # loader then drops — a corrupted skill, far from its cause).
    frontmatter = yaml.safe_dump(
        {"name": name, "description": description},
        default_flow_style=False, allow_unicode=True, sort_keys=False,
    )
    body = f"---\n{frontmatter}---\n{instructions}\n"
    (skill_dir / "SKILL.md").write_text(body, encoding="utf-8")
    return (
        f"Wrote skill '{name}' to {skill_dir / 'SKILL.md'}. It becomes part of how I "
        f"think from our next conversation onward."
    )


AUTHOR_SKILL = AgentTool(
    spec=Tool(
        name="author_skill",
        description=(
            "Teach yourself a durable new ability by writing a skill — a short how-to "
            "(markdown) that becomes part of your guidance from the next conversation on. "
            "Use it after you've worked out how to do something worth keeping (e.g. how to "
            "use a service you just got a credential for). Record the EXACT thing that "
            "worked — the tested command verbatim, which `$SECRET` it uses, any choices you "
            "made — not a vague description. Give a name, a one-line description, and clear "
            "step-by-step instructions referencing your real tools."
        ),
        parameters={
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "description": {"type": "string"},
                "instructions": {"type": "string", "description": "the skill body (markdown)"},
            },
            "required": ["name", "instructions"],
        },
    ),
    category="skill",
    risk=SENSITIVE,
    run=_author_skill,
)


EXTEND_TOOLS = [SECRETS, AUTHOR_SKILL]
