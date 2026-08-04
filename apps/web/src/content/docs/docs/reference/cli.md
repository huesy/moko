---
title: CLI reference
description: Every moko command, grouped by what you'd reach for it.
---

`moko` is how you set up, run, and look after your companion. Everything is below.

:::tip
Run `moko <command> --help` for the full options on any command.
:::

## Setup & configuration

| Command | What it does |
| --- | --- |
| `moko setup` | Guided first run (full-screen, stepped) — language, country & [sleep window](/docs/concepts/dreaming), [brain & senses](/docs/install), Telegram. |
| `moko config [KEY [VALUE]]` | Read or change settings. No args opens an editor; `--list` shows all; `--restart` restarts after. |
| `moko secret [set\|list\|rm] [name]` | Store provider credentials in `~/.moko/`. Moko uses them but never sees the value. |
| `moko login [provider]` | Connect a provider sign-in or subscription (e.g. `xai`) instead of a metered key. |

## Status & health

| Command | What it does |
| --- | --- |
| `moko status` | How Moko is configured. |
| `moko doctor` | Local health check — what's set up, running, or needs attention. |
| `moko journal` | Recent [dreams](/docs/concepts/dreaming) and unprompted messages. |

## Talking to Moko

| Command | What it does |
| --- | --- |
| `moko chat` | Talk in the terminal — handy when the service is off. |
| `moko pair` | Connect [Telegram](/docs/install#3-pair-telegram) with a one-time code. |

## The background service

| Command | What it does |
| --- | --- |
| `moko start` | Bring Moko online on every configured channel. |
| `moko service [install\|start\|stop\|restart\|status\|logs\|uninstall]` | Manage the background service (`systemd --user` on Linux). Defaults to `status`. |

## Nudge its inner life

Trigger now what Moko normally does on its own — handy for trying it out.

| Command | What it does |
| --- | --- |
| `moko dream` | [Consolidate memory](/docs/concepts/dreaming) now. |
| `moko reflect` | Form a private [thought](/docs/concepts/inner-life) now. |
| `moko research` | Look up one of [its own questions](/docs/concepts/curiosity) now. |
| `moko outreach [--force]` | Run the [reach-out decision](/docs/concepts/proactivity); `--force` just sends. |

## Trust & the mind

| Command | What it does |
| --- | --- |
| `moko trust [category] [allow\|ask\|deny]` | View or set [per-category trust](/docs/safety#per-category-trust) for sensitive actions. |
| `moko snapshot [label]` | Back up the whole mind now; `--list` shows backups. |
| `moko restore [name]` | Restore from a snapshot (**stop Moko first**); `--yes` skips the prompt. |

## Maintenance & the monitor

| Command | What it does |
| --- | --- |
| `moko groom [--upgrade]` | Refresh tokens, the `claude` CLI, and updates; `--upgrade` applies Moko updates. |
| `moko monitor [--no-open]` | Open the local [monitor](/docs/reference/monitor); `--no-open` just prints the URL. |
