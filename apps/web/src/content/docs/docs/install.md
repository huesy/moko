---
title: Install & setup
description: Install Moko, set your timezone and LLM, pair Telegram, and bring it online.
---

Getting Moko running takes a few minutes. The installer brings everything it needs — including a private copy of Python — so the only prerequisite is a terminal. Works on macOS, Linux, and Windows.

## 1. Install

```sh
# macOS / Linux
curl -fsSL https://mokomoko.ai/install.sh | sh
```

```powershell
# Windows
irm https://mokomoko.ai/install.ps1 | iex
```

- **Safe by default.** Installs the `moko` command into your user directory — never as root, never touching system files.
- **Pin a version.** Set `MOKO_VERSION=v0.1.0` (or any release tag) before the command for a reproducible setup. Otherwise you get the latest release.

### From source

With [uv](https://docs.astral.sh/uv/), install the CLI straight from the repo:

```sh
uv tool install --from "git+https://github.com/huesy/moko.git#subdirectory=apps/agent" moko
```

Or work inside a clone:

```sh
cd apps/agent
uv sync
uv run moko --help
```

## 2. Set up

```sh
moko setup
```

A warm, full-screen walkthrough — a handful of steps with a progress trail across the top — that asks for:

- **Your language** — Moko greets you, and talks with you, in it. The setup screens themselves are fully translated for a few languages and fall back to English for the rest; either way Moko converses in your language.
- **The terms** — a short, plain-language note on how Moko works: it never lies to you, your data stays on your machine, and it asks before doing anything that reaches the outside world. You accept before going on.
- **Your country** — Moko is always time-aware, and everything it does is stamped in *your* time. Your country sets the timezone (with a quick city pick where a country spans several). Nothing is hard-coded; it learns this here.
- **Your sleep window** — the quiet hours when Moko won't message you and instead [dreams](/docs/concepts/dreaming): the night it spends consolidating what it learned.
- **Your LLM provider** — bring your own. Anthropic, OpenAI, OpenAI-compatible endpoints (including xAI Grok), or a Claude/Grok **subscription** via OAuth (`moko login`) instead of a metered API key.
- **Senses (optional)** — point image, voice, and video at their own providers if you want, so Moko can [send and make media](/docs/concepts/senses).
- **How you'll talk** — connect Telegram right here, so it's all one sitting (more channels are on the way).

Store credentials with `moko secret set`, then sanity-check:

```sh
moko status     # how Moko is configured
moko doctor     # scan what's set up, running, or needs attention
```

## 3. Pair Telegram

Talk to Moko from your phone. Pairing is secured by a one-time code, so a stranger who finds the bot can't bind it.

```sh
moko pair
```

Follow the prompt: message your bot, confirm the code, and from then on only you are heard. Prefer the terminal? Just run `moko chat`.

## 4. Bring Moko online

Run Moko as a background service so it's always there — surviving restarts, ticking its [heartbeat](/docs/concepts/proactivity), and [dreaming](/docs/concepts/dreaming) overnight.

```sh
moko service install     # set up the background service
moko service start       # bring it online
moko service status      # check on it
moko service logs        # tail the logs
```

`moko setup` offers to do this at the end, so in practice it usually just works.

## 5. Watch Moko's mind (optional)

```sh
moko monitor
```

Opens the local [monitor](/docs/reference/monitor) — a live window into Moko's feed, thoughts, subjects, curiosities, commitments, and dreams. Served on `127.0.0.1`; never leaves your machine.

## Where everything lives

- **One place.** Moko's entire mind — the graph, your config, logs, and secrets — lives under `~/.moko/`, never in a repo or the cloud.
- **One file.** The mind itself is a single SQLite file. Snapshot it any time with `moko snapshot`, restore with `moko restore`.
