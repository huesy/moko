---
title: Dreaming
description: While you sleep, Moko consolidates its memory, reflects, and leaves a journal.
---

While you rest, Moko **dreams**. The quiet hours when it stays silent aren't idle time — they're when it tidies up everything the day gathered.

The day gathers loosely. The night makes it coherent.

## What a dream does

- **Tidies memory** — defragments the [graph](/docs/concepts/memory-graph) and recomputes derived state.
- **Reconciles subjects** — reviews the day's merge candidates with full context, then confirms, rejects, or flags the genuinely ambiguous ones.
- **Reflects** — lets [thoughts](/docs/concepts/inner-life) fade in weight (never deleted), and re-scores which [knowledge gaps](/docs/concepts/curiosity) matter most for tomorrow.
- **Upkeep** — reindexes search and runs only *safe* maintenance.

:::note
Anything that would change Moko's own code or your system stays consent-gated. The dream only *executes* what you already approved during the day — it never silently rewrites itself.
:::

## A fresh head

Consolidating also ends the active session. Moko wakes with a small, clean working memory — and none of the knowledge lost, because it all lives in the graph now.

## The dream journal

Every dream leaves a short record of what it consolidated and what it now wonders about, so the night is auditable.

```sh
moko journal     # read the journal
moko dream       # run a consolidation by hand
```
