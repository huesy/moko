---
title: Time awareness
description: Moko always knows the date, time, and weather where you are, and time-stamps everything it does.
---

Moko lives in time. It always knows what time it is in *your* timezone — and what the weather's like outside your window. That sounds small, but almost everything else leans on it.

## Everything is time-stamped

- **Every message, fact, thought, and action** carries a timestamp.
- So Moko reasons about *when*, not just *what* — "we talked about this last Tuesday," "that fact is six months old now."

## Stored in UTC, lived in your timezone

- **Saved in UTC** so travel and daylight-saving never corrupt the record.
- **Shown in your timezone** so what you see is always local to you.

:::note
Anything that depends on "now" — *recent*, *overdue*, *tomorrow*, your [dream hours](/docs/concepts/dreaming), the [outreach](/docs/concepts/proactivity) budget, [fact staleness](/docs/concepts/curiosity) — is computed live, never guessed.
:::

## It knows your weather too

- **Keyless and city-only** — just your town, never GPS.
- It even shows up in Moko's [photos and selfies](/docs/concepts/senses): the scene matches your real time of day and weather.

## You set it once

Your timezone and sleep window are learned at [setup](/docs/install) and kept in config — never baked into Moko's rules. Change them any time with `moko config`.
