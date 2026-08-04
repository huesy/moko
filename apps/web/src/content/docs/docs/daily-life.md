---
title: Daily life with Moko
description: What living with Moko is actually like, day to day.
---

Once Moko is online, you mostly just talk to it — on Telegram or in the terminal. Everything below happens quietly in the background of that.

## It remembers, exactly

You don't manage Moko's memory. You talk, and it learns.

- **Mention people and places** and it records them — *"dinner with my sister Maya and her husband Tom in Wellington"* becomes Maya, Tom, and Wellington in its graph.
- **Every fact is stamped** with when it learned it and where it came from.
- **Ask later** — *"what was Maya's husband called again?"* — and you get a straight answer.

See [the memory graph](/docs/concepts/memory-graph).

## It wonders about you

Moko notices what it doesn't know and gets curious.

- **It asks gently**, at a natural moment — never a quiz.
- **It answers itself first**, looking things up when the world can settle them.
- **It only brings you** the genuinely personal questions.

See [curiosity](/docs/concepts/curiosity).

## It reaches out — within limits

Sometimes Moko messages first: to follow up, to flag tomorrow's appointment, or to check in if you've seemed off.

This is governed. It stays quiet during your [sleep window](/docs/concepts/dreaming), won't talk over a live conversation, caps how curious it gets in a day, and doesn't nag. See [proactivity & the governors](/docs/concepts/proactivity).

## It holds what you ask

Say *"remind me about the dentist on Friday"* and Moko records a real **commitment** with a due time — then surfaces it when it's due, in your timezone. Not a hollow *"noted."*

## It has senses

Moko can see, make, hear, and speak.

- **Send it a photo** and it actually sees what's in it.
- **Ask for a picture** — a selfie in its current mood, a photo of a scene it wants to show, or anything you describe.
- **It sends voice notes** and **hears yours**.

Picture, video, and voice generation cost money, so Moko asks before spending. Outbound photos have GPS and EXIF stripped.

:::note
Its selfies stay recognisably on-model, and the photos it takes reflect your real local time and weather.
:::

## It acts, with your say-so

Moko can run commands, read and search files, and fetch pages on its own.

Anything **sensitive** — messaging someone else, spending money, touching system files, deleting things — it names first and waits for your go. Tune its latitude per category with `moko trust`. See [the safety model](/docs/safety).

## It dreams overnight

During your quiet hours, Moko [dreams](/docs/concepts/dreaming): tidying its graph, merging duplicate people, re-weighing what it cares about, and leaving a short **journal** of what it consolidated. You wake to a clearer-headed Moko.

## You can watch it think

Run `moko journal` for recent dreams and the messages Moko sent on its own, or open the [monitor](/docs/reference/monitor) for a live view of its feed, thoughts, curiosities, and commitments.

Its inner life is private by default — but it's yours to look in on.
