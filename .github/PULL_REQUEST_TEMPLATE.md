<!--
Thanks for contributing to Moko! Please read CONTRIBUTING.md first.
Keep PRs small and focused — small and durable beats clever.
-->

## What & why

<!-- What does this change, and what problem does it solve? -->

Closes #

## How it was tested

<!-- Which checks did you run? Paste results / note any failures honestly. -->

- [ ] `uv run ruff check .` (in `apps/agent/`)
- [ ] `uv run pytest -q` (in `apps/agent/`)
- [ ] `pnpm --filter @moko/web build` (if the site/docs changed)
- [ ] `pnpm --filter @moko/monitor build` (if the dashboard changed)

## Checklist

- [ ] I read the relevant referee docs (MISSION / SCHEMA / ROADMAP).
- [ ] No secrets, private data, or `~/.moko/` state is committed.
- [ ] New facts entering the graph carry a source + timestamp; no hard-coded
      timezone or other companion-specific values.
- [ ] Safety floors (honesty boundary, consent gate, governors) are intact, with
      tests if I touched them.
- [ ] Any new migration is a new numbered file (existing migrations untouched).
- [ ] `CHECKLIST.md` updated if this lands a roadmap item.
