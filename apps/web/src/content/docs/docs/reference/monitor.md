---
title: The monitor
description: A local web window for watching Moko think and browsing its mind, served only on your machine.
---

The monitor is a small local web app: a live feed of what Moko is doing *and* a browser for everything in its mind. It runs on `127.0.0.1` and never leaves your machine.

```sh
moko monitor           # open it in your browser
moko monitor --no-open # just print the URL
```

:::note
Moko has to be online to watch — the background [service](/docs/reference/cli#the-background-service) running, or `moko start`.
:::

## Watch it think

- **Live feed** — events as they happen: messages in and out, facts learned, thoughts formed, curiosities raised, actions taken.
- **Rhythm bar** — the day at a glance: quiet hours, outreach budget, heartbeat.
- **Dreams** — the dream audit: every nightly [consolidation](/docs/concepts/dreaming), with its summary, Moko's private reflection, and what each step touched.
- **Usage** — tokens spent by activity (reply, vision, dream, outreach, research), over the last day, week, or all time.
- **Media** — the pictures Moko has made and received.
- **Access tiers** — your current per-category [trust](/docs/safety#per-category-trust) settings.

## Browse its mind

The brain browser is the other half — it walks the same store Moko reasons from.

- **Thoughts** — its private [inner life](/docs/concepts/inner-life), with each one's mood and how it's faded.
- **Subjects** — the people, places, and things in [the graph](/docs/concepts/memory-graph), and the facts and relationships on each.
- **Gaps** — what Moko is [curious](/docs/concepts/curiosity) about, and how it scored it.
- **Commitments** — what it's holding for you, and when it's due.
- **Outreach** — when it reached out on its own, and when it weighed it and held off.
- **Messages** — the conversation record.

## Private by design

It binds to loopback only, so it's never reachable from the network. Your world, your machine.
