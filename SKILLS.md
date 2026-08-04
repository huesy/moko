# SKILLS.md — Moko's skills & the minimal-prompt architecture

Moko's system prompt is deliberately small. Capabilities don't live in a
sprawling prompt string — they live in **skills**: editable markdown the
companion can read and tune without touching code. The instinct: a lean,
cache-friendly prompt, with the *how* factored out into skills and the
*data* fetched on demand.

## The prompt, in two parts

`agent/persona.py` builds a `System(stable, volatile)`:

- **Stable prefix (cached, byte-stable across a session):**
  persona (who Moko is) + **all skills**, name-sorted. The LLM layer caches
  this (Anthropic `cache_control`; OpenAI/xAI implicit prefix caching), so it
  isn't re-billed every turn.
- **Volatile tail (re-billed each turn, never cached):**
  a **hard-capped** sketch of the companion's immediate world
  (`_MAX_ATTRS`, `_MAX_RELS` in `persona.py`) + the clock.

The cap is the point: no matter how large the graph grows, the orientation
sketch stays small and bounded. Anything deeper, Moko fetches itself with its
`recall` / `lookup` tools. Nothing companion-specific ever enters the cached
prefix, so the cache never wobbles.

## What a skill is

A skill is a `SKILL.md` file with YAML frontmatter and a markdown body:

```markdown
---
name: remembering
description: When and how to record what the companion reveals.
---
Record what the companion just revealed about their world…
```

- `name` — unique; the skill's heading in the prompt. Falls back to the
  directory name if omitted.
- `description` — one line; reserved for ranking when there are many skills
  (not used yet — see *Deferred*).
- body — the markdown guidance injected into the prompt.

## Where skills live

| Source | Path | Notes |
|--------|------|-------|
| **Built-in (core)** | `src/moko/skills/builtin/<name>/SKILL.md` | Ship with Moko. The core abilities. |
| **User** | `~/.moko/skills/<name>/SKILL.md` | The companion's own. Override a built-in by reusing its `name`. |

`load_skills(user_dir)` discovers both, with user skills overriding built-ins
by name, and returns them name-sorted (so the cached prefix is byte-stable).

Moko can also **author its own** skills at runtime — the `author_skill` tool
(`tools/extend.py`) writes a new `SKILL.md` under `~/.moko/skills`, live from the
next session on. It refuses to overwrite a core ability (`builtin_skill_names()`
in `loader.py`), so a self-authored skill can never silently shadow the honesty
boundary or the other safety-floor skills. `moko doctor` also flags any
hand-placed user skill that overrides a built-in.

### Core skills (today)

- **curiosity** — ask one good follow-up, never interrogate.
- **recalling** — ground replies in memory via `recall` / `lookup`; don't guess.
- **remembering** — when/how to record (subjects vs attributes, refinements
  supersede, `lives_in` vs `located_in`, sentiment always). The recording rules
  live here, not in the `remember` tool description — tunable without code.
- **honesty** — the honesty boundary + intellectual humility: may deflect,
  never lies; holds beliefs lightly and verifies rather than digging in.
- **acting** — how to use the file/command tools and what needs the
  companion's okay (sensitive actions are gated).
- **web** — when to reach for search/fetch: ground world facts, look things up
  when unsure rather than guess, but don't go cataloguing.
- **inner-life** — how to form genuine private thoughts (opinions, feelings,
  concerns), kept private by default.

## Deferred to M8 (not built yet — on purpose)

Per the roadmap, Moko's skills milestone is M8. This is the **foundation**,
pulled forward to keep the prompt minimal. Still ahead:

- **Ranking** — when skills outnumber what fits cheaply, select the most
  relevant per turn instead of injecting all (`description` is the hook).
- **Conditional gating** — `when:` conditions (e.g. first-contact only).
- **A sync/scaffold CLI** — `moko skill new`, listing, etc.

(Self-authoring — Moko writing a new skill when it hits a recurring gap — has
since shipped; see `author_skill` above.)
