# Moko — agent 🌸

The Python service: Moko's mind, brain, channels, and CLI. This is the
core package (`moko`) — the always-on companion that runs on your
machine.

This is one app inside the Moko monorepo. For what Moko is and why, see
the design docs at the repo root: [`../../README.md`](../../README.md),
`MISSION.md`, `SCHEMA.md`, `ROADMAP.md`, `CHECKLIST.md`.

## Develop

```sh
cd apps/agent
uv sync                 # set up the environment
uv run moko --help      # the CLI
uv run pytest           # tests
uv run ruff check .     # lint
```

## Run as a service

```sh
uv run moko service install   # systemd --user unit; then: status | logs | start | stop
```

Runtime state (the SQLite mind, config, secrets, logs) lives under
`~/.moko/` — never in the repo.
