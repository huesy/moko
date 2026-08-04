# Contributing to Moko 🌸

Thanks for wanting to help build Moko. This is a small, opinionated codebase that
values being **correct, small, durable, and honest about its limits** over being
clever. A few things to know before you dive in.

## Read the referee docs first

Moko is governed by design docs at the repo root. Before proposing a change,
skim these — a PR that conflicts with them will be sent back:

- **[`MISSION.md`](MISSION.md)** — what Moko is, the curiosity/proactivity model,
  the honesty boundary, and the four safety floors. When anything disagrees with
  the mission, the mission wins.
- **[`SCHEMA.md`](SCHEMA.md)** — the memory-graph design. Understand it before
  touching anything that writes to the store.
- **[`ROADMAP.md`](ROADMAP.md)** + **[`CHECKLIST.md`](CHECKLIST.md)** — what's
  built, what's next, and the order.
- **[`CLAUDE.md`](CLAUDE.md)** — the engineering conventions in full.

## Project layout

Monorepo. Two independent toolchains — there is no unified build.

- `apps/agent/` — the Python service (Moko's mind, brain, channels, CLI). **uv**.
- `apps/web/` — the marketing site + docs (Astro/Starlight). **pnpm**.
- `apps/monitor/` — the local dashboard (Vite + React). **pnpm**.

Runtime state lives under `~/.moko/` — **never** in the repo.

## Setting up

**The agent (Python ≥ 3.12, via [uv](https://github.com/astral-sh/uv)):**

```sh
cd apps/agent
uv sync --extra dev        # install deps + dev tools (ruff, pytest)
uv run moko --help
```

**The web + monitor (Node 22, pnpm):**

```sh
pnpm install               # from the repo root
pnpm dev                   # the website
pnpm dev:monitor           # the dashboard SPA
```

## Before you open a PR

Run the same checks CI runs, and make them pass:

```sh
# agent
cd apps/agent
uv run ruff check .
uv run pytest -q

# web + monitor
pnpm --filter @moko/web build
pnpm --filter @moko/monitor build
```

Report failures honestly — a PR with a red test and a note beats a green one that
hides a skip.

## House rules that matter here

- **The safety floors are code, not vibes.** The honesty boundary, the consent
  gate, and the quiet-hours/budget governors are enforced at real choke points
  with tests that prove they hold. Don't weaken them without a very good reason
  and a test.
- **Every fact is sourced and time-stamped.** Nothing enters the graph without
  provenance and a timestamp. Store timestamps in **UTC**; present/reason in the
  configured timezone. **Never hard-code a timezone** (or any companion-specific
  value) — it's config, learned at setup.
- **Migrations are append-only.** Numbered `.sql` files in `apps/agent/src/moko/
  migrations/`, applied in order, **never edited after they ship**.
- **The store is the only thing that touches SQLite.** Go through the `store/`
  layer.
- **Async everywhere; no blocking I/O on the loop thread.** (ruff's `ASYNC` rules
  are on.)
- **LLM/provider logic stays behind `llm/`.** Never hard-code a model id outside
  config; default to the latest, most capable models.
- **Match the surrounding code's idiom** — its naming, comment density, and
  structure. Read before you write.
- Style: **ruff** (line-length 100, `E,F,I,B,UP,SIM,ASYNC`). Tests:
  **pytest + pytest-asyncio**; the store and graph carry real coverage, LLM and
  network calls are faked at the interface seam.

## Commits & PRs

- Branch off `main` for non-trivial work.
- Keep [`CHECKLIST.md`](CHECKLIST.md) honest — tick items off only when they're
  truly built + tested.
- Fill in the PR template. Link the issue it closes.

## Reporting bugs & security

- Bugs / ideas: open an [issue](https://github.com/huesy/moko/issues) using a
  template.
- **Security issues: do not use a public issue** — see [`SECURITY.md`](SECURITY.md).

By contributing, you agree your work is licensed under the repo's
[MIT License](LICENSE).
