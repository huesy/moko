---
title: Curiosity
description: How Moko notices what it doesn't know and answers its own questions before asking you.
---

Curiosity is what makes Moko feel alive. It keeps track of what it doesn't know yet, and quietly works to fill those gaps.

## What it gets curious about

- **What's missing** — a subject it barely knows, an unconfirmed relationship, a dangling reference (*"my colleague"* — which one?), a fact gone stale.
- **Why** — once it learns the *what*, it often wonders about the reason or story behind it.
- **Whether it's true** — surprising, contradictory, or low-confidence claims get a *"is that real?"* instead of being swallowed whole.

That skepticism points at *the world* — online claims, stale facts — not at you. If something you said seems off, Moko asks gently (*"I had it as Tuesday — did that move?"*), never accusing.

## Not every gap matters

Moko scores each gap by how interesting, useful, or relationship-deepening it is, and how long it's sat open. The high-value ones rise; the rest wait.

## It answers itself first

:::tip
This is what keeps curiosity from becoming nagging.
:::

For anything the world can settle, Moko **researches first** — web search and fetch, its own tools, cross-checking its [graph](/docs/concepts/memory-graph). It only brings a question to you when the answer is genuinely **personal** (only you know it).

:::note
Research *during a conversation* is always on. Letting Moko chase its open questions **unprompted**, on its own idle time, is opt-in — off by default (it pulls in untrusted web content), enabled at setup or with `moko config curiosity.autonomous_research on`.
:::

## How it asks you

- **In the flow** of conversation when there's a natural opening.
- **Unprompted** when there isn't — but only within the [proactivity governors](/docs/concepts/proactivity).
- Gently, spaced out, never a quiz.

## It compounds

Every answer feeds the graph with its source, and may open new gaps. The more Moko knows, the sharper its questions get.
