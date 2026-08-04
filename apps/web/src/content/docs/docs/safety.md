---
title: The safety model
description: The floors, the consent gate, and the per-category trust that keep Moko's power bounded.
---

Moko acts in the world — runs commands, reads files, fetches pages. That power sits on a few floors that are **code, not vibes**, with tests that prove they hold.

## The four floors

- **No lies.** Moko may keep things private or deflect, but never fabricates a fact or denies a known truth. See [the honesty boundary](/docs/concepts/inner-life).
- **Sensitive actions need your go.** Sending, spending, deleting, touching system state — Moko names the intent and waits. Reading, searching, computing are free.
- **Your data stays put.** The mind is a local SQLite file; nothing leaves your machine except through actions you authorize.
- **Prompt injection is real.** Pages, email, and files from others are untrusted and can try to steer Moko. The consent gate exists precisely for this.

## The consent gate

- **Free** (no asking): reading, searching, computing, web fetch/search, recalling its own memory.
- **Gated** (named, then waits): messaging other people, spending money, writing or deleting files, running commands that change things, touching system state.
- **Spend-gated too:** generating images, video, or voice asks first, like any other sensitive action.

When Moko hits a gated action it parks it, says plainly what it wants and why, and waits — a tap on Telegram (✅ / ❌), yes/no in the terminal. Every approval is logged.

:::note
Outbound photos have their GPS and EXIF metadata stripped before they ever leave your machine.
:::

## Per-category trust

Tune how much latitude Moko gets, per category, with `moko trust`:

```sh
moko trust                 # view current trust per category
moko trust files allow     # stop asking before file writes
moko trust system ask      # always ask before touching system state
```

`allow` waves a category through, `ask` gates it each time, `deny` blocks it.

## Running Moko safely

- Run it on a machine **you** trust — ideally full-disk encrypted, since the mind holds everything Moko knows about you.
- Snapshot before big changes with `moko snapshot`, restore with `moko restore`. Moko also auto-snapshots before risky maintenance.
- The [monitor](/docs/reference/monitor) is bound to `127.0.0.1` — for you, on your machine, not the network.
