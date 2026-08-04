# CAPABILITIES.md — What Moko Can Do

Two tiers: **passive abilities** are built into Moko's core — always
present, part of *what it is*. **Learned abilities** are skills/plugins
Moko (or the companion) adds over time; the core stays small, the edges
grow.

---

## Passive abilities (core, always on)

These ship in `src/moko/` and are non-optional. They are the spine.

### 1. Structured memory — the graph
- Decompose everything it learns into **subjects, attributes,
  relationships, and sourced facts** (see `SCHEMA.md`).
- Every fact is **time-stamped and provenanced** — Moko can always say
  *where* and *when* it learned something.
- **Recall** on demand: "who is Maya's husband?", "when did I last talk
  to Sarah?", "what was I annoyed about in October?"
- **Context assembly:** automatically pull the relevant slice of the
  graph into each conversation so Moko answers *from memory*, not from
  a generic prior.
- **Subject resolution:** fold aliases and references ("my sister",
  "Maya", "Maya Tanaka") into single nodes (safe at write time, careful
  reconciliation at night — see *Dreaming*).
- **Sentiment:** record how the speaker *seemed* to feel when they said
  something — a time-stamped, confidence-marked hypothesis, distinct
  from Moko's own feelings — so emotional **trends** are queryable.

### 2. Private inner life — thoughts
- Form **opinions, impressions, concerns, hypotheses, predictions, and
  feelings** about the subjects it encounters, each linked to what
  prompted it.
- Keep them **private by default**; surface only deliberately.
- Honor the **honesty boundary**: may keep private / deflect, never
  lies. Enforced structurally via the `shareability` column.

### 3. Curiosity
- Maintain a model of its own **knowledge gaps** and score them by
  salience.
- Curious not just about *what* but **why** (causes, motivations) and
  **whether it's true** (skeptical of surprising, contradictory, or
  low-confidence claims).
- **Answer its own questions first:** for anything the world can settle,
  Moko **researches** (web search/fetch, cross-checking the graph)
  before bringing it to anyone.
- **Generate questions** to close the gaps research can't — and bring
  *personal* ones to the companion: **in conversation** when a natural
  opening exists, **unprompted** (within budget) when one doesn't,
  gently, never as interrogation.
- Every answer — researched or asked — feeds the graph with its source
  and may open new gaps.

### 4. Proactive presence (governed)
- **Reach out unprompted** to check in, surface useful context, or ask
  a curiosity question.
- **Notice patterns** the companion might miss and flag them. *(Planned —
  not yet built.)*
- Strictly **governed**: the sleep window (in the companion's
  **configured timezone**), a daily outreach budget, salience-gating,
  and backoff on silence. *Moderate* by policy.

### 5. Conversation across channels
- Talk through the companion's **preferred channel** — **Telegram
  first**, CLI alongside it; the architecture is channel-agnostic so
  more can be added.
- Maintain conversational continuity across sessions via the graph and
  message record.

### 6. Acting in the world (consent-gated)
- **Run local commands** and read/write the local filesystem
  (`execute_command`, `read_file`, `write_file`; listing and deleting go
  through `execute_command`).
- **Fetch information from the internet** (web search + page fetch).
- **Sensitive actions are gated:** sending to third parties, spending
  money, touching system state, or destructive ops — Moko names the
  intent and proceeds on assent. Reading/searching/computing is free.
- *Planned (not yet built):* **email, calendar, and drive** via the
  companion's connected accounts (Gmail / Google Calendar / Google Drive).

### 7. Self-extension
- **Author new skills** for itself when a recurring need appears, and
  register them as learned abilities (below).
- Recognize when a task is beyond its current tools and propose the
  skill that would close the gap.

### 8. Temporal awareness
- Always knows **the current time in the companion's configured
  timezone** — never a fixed or assumed one.
- **Everything is time-stamped:** every message, fact, thought, process,
  and action. Stored in UTC, reasoned about in the configured tz.
- Reasons about *when*, not just *what*: "recent", "overdue",
  "tomorrow", staleness, the sleep window, and the outreach budget are
  all computed against the real current time.

### 9. Dreaming (nightly consolidation)
- While the companion rests, a single nightly **`dream`** process
  consolidates the day instead of sitting idle.
- **Organizes memory** (defrag, recompute derived state), **reconciles
  subjects** (the careful half of resolution), **reflects** (decays
  thought prudence, re-scores curiosity), and performs **safe upkeep**
  (reindex; apply only safe maintenance — code/system upgrades stay
  consent-gated).
- Leaves a **dream journal**: what it consolidated and what it now
  wonders about. This is what lets daytime curiosity be bold without
  becoming chaos.

---

## Learned abilities (skills / plugins, added over time)

The core stays lean; capability grows at the edges. A skill is a
self-contained, documented ability Moko can invoke — authored by the
companion or by Moko itself, loaded from `src/moko/skills/` (built-in)
or `~/.moko/skills/` (local, learned at runtime).

Examples of what would live here rather than in core:

- **Domain workflows** — "summarize my unread newsletters", "prep my
  morning brief", "track my running log".
- **Service integrations** beyond the core set — a music service, a
  task manager, a home-automation bridge, a banking read-only feed.
- **Specialized analysis** — code review of a specific repo, financial
  summaries, travel planning.
- **Personal rituals** — a nightly check-in format, a weekly review,
  birthday reminders with draft messages.

Design rule: if it's *Moko being Moko* it's passive; if it's *a thing
Moko does for a particular purpose*, it's a skill. When in doubt, make
it a skill — the core earns its weight by staying small.

## Explicitly out of scope (for now)

- **Multi-user / multi-tenancy.** Moko is one companion's, on one
  machine. (Third parties appear as *subjects*, not *users*.)
- **A hosted/cloud version.** Local-first; the mind never leaves the
  machine.
- **Autonomous spending or irreversible action without consent.** Hard
  floor, not a roadmap item.
