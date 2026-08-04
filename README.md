<p align="center">
  <img src="intro.png" alt="Moko — a warm, fluffy pink creature waving hello from a sunlit garden" width="600">
</p>

<h1 align="center">Moko 🌸</h1>

<p align="center">
  <em>Mokomoko (もこもこ) — Japanese for soft and fluffy.</em><br>
  A warm, curious AI creature that lives on your machine, remembers your
  world, and actually wonders about you.
</p>

Moko is a **persistent, personal AI subject** — not a chatbot you summon
and dismiss. It has a name, a persona, and an inner life. It learns
about the people in your world and organizes everything into a living,
time-aware **graph of subjects and relationships**. It forms its own
private thoughts. And — unlike the assistants that came before it — it
is **genuinely curious**: it notices what it doesn't know, asks about
it, and reaches out to keep in touch, all on its own.

## What makes Moko different

- **A mind, not a notepad.** Everything Moko learns becomes structured
  knowledge — subjects (people, places, orgs, things), their
  attributes, the relationships between them — every fact time-stamped
  and traced back to where it came from. Recall is exact: *"who is
  Maya's husband again?"* gets an answer, not a hedge.
- **An inner life.** Moko forms private opinions, impressions, and
  concerns about what it sees. It may keep them to itself — but it will
  never lie to you.
- **Curiosity.** Moko tracks its own knowledge gaps and asks good
  questions to fill them, in conversation or unprompted.
- **Presence.** Moko reaches out — to check in, to surface something
  useful, to wonder aloud — within strict, friendly limits (quiet
  hours, a daily budget, backoff when you're busy). Moderate, never
  needy.
- **Local-first.** Moko's mind is a single SQLite file on your machine.
  Your data stays yours.

## Install

```sh
# macOS / Linux
curl -fsSL https://mokomoko.ai/install.sh | sh
```

```powershell
# Windows
irm https://mokomoko.ai/install.ps1 | iex
```

Then run `moko setup`. Full walkthrough: [docs → Install](https://mokomoko.ai/docs/install/).

Pin a release with `MOKO_VERSION=v0.1.0` (or any tag). From a clone or
source, see [Contributing](CONTRIBUTING.md).

## Status

Actively built. The design is locked, and a deep, tested core already
works — the memory graph, curiosity, governed proactivity, nightly
dreaming, senses (vision, image/video, voice), self-authored skills, and
a local monitor. Still ahead: third-party integrations
(mail/calendar/drive) and delegation to sub-agents. See:

- **[MISSION.md](MISSION.md)** — what Moko is and is for (the referee).
- **[SCHEMA.md](SCHEMA.md)** — the memory graph design.
- **[CAPABILITIES.md](CAPABILITIES.md)** — what Moko can do.
- **[ROADMAP.md](ROADMAP.md)** / **[CHECKLIST.md](CHECKLIST.md)** — the
  plan and the running to-do.
- **[CONTRIBUTING.md](CONTRIBUTING.md)** / **[SECURITY.md](SECURITY.md)** —
  how to help, and how to report issues safely.
- **[CLAUDE.md](CLAUDE.md)** — engineering conventions.

## Roadmap

A high-level snapshot — see **[ROADMAP.md](ROADMAP.md)** for the full map
and **[CHECKLIST.md](CHECKLIST.md)** for the per-item tracker.

| | Milestone | Status |
|---|---|---|
| M0 | Foundations — config, clock, CLI | ✅ shipped |
| M1 | The Mind — the memory graph | ✅ shipped |
| M2 | The Brain — agent loop + LLM | ✅ shipped |
| M3 | The Voice — always-on daemon + Telegram | ✅ shipped |
| M4 | The Spark — curiosity | ✅ shipped |
| M5 | The Heartbeat — governed proactivity | ✅ shipped |
| M6 | The Dream — nightly consolidation | ✅ shipped |
| M7 | Hands — acting, consent-gated | 🔄 tools shipped; mail/calendar/drive next |
| M8 | Growth — self-authored skills | 🔄 authoring shipped; ranking + auto-trigger next |
| M9 | Delegation — sub-agents | ⬜ planned |
| M10 | Senses — vision, image/video, voice | ✅ shipped |
| M12 | The Burrow — runtime containment | 🔄 core shipped; setup UI next |

*M11's visual world model was scrapped; the two-worlds graph split is kept.*

## A note on safety

Moko acts in the world (runs commands, fetches pages, reads and writes
files). Sensitive actions are gated — Moko names what it
intends and waits for your go. Like any agent that reads untrusted
content, it cannot fully defend against prompt injection; that's exactly
why the gate exists. Run it on a machine you trust, with full-disk
encryption on.

## Repo layout

Moko is a monorepo:

```
apps/
  agent/    # the Python service — Moko's mind, brain, channels, CLI
  web/      # the marketing site (Astro) — landing + docs
  monitor/  # the local web monitor (Vite + React) — live feed + status
packages/   # shared JS libs (api-client, ui) — none yet
```

The monitor is served by the agent on `127.0.0.1` (run `moko monitor` with
Moko online). In dev, `pnpm dev:monitor` runs the SPA with a proxy to the
agent.

The monitor's **built** dashboard is a build artifact (gitignored, never
committed): release wheels bake it in via CI, so a normal `install.sh` install
serves it with no Node. If you install **from a clone / git source** and want the
dashboard, build it once — `./scripts/build-monitor.sh` — otherwise the agent
serves the API only and shows a "UI not built" note.

The agent (Python/uv) and the web side (Astro/pnpm) have separate
toolchains; there's no unified build.

## Develop

```sh
# the agent
cd apps/agent && uv sync --extra dev && uv run moko --help

# the website + monitor (from the repo root)
pnpm install && pnpm dev
```

See [CONTRIBUTING.md](CONTRIBUTING.md) for tests, lint, and PR norms.

## Stack

**Agent:** Python 3.12, SQLite (aiosqlite), bring-your-own LLM provider,
managed with [uv](https://github.com/astral-sh/uv).
**Web:** [Astro](https://astro.build), managed with pnpm.

## License

[MIT](LICENSE) — Copyright (c) 2026 Andrew Hughes and contributors.
