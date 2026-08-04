# SUBAGENTS.md — Moko's delegation model

When a task is big, long-running, or needs sustained focus — build a website,
research a topic deeply, set up a service — Moko delegates to a **sub-agent**: a
focused worker with its own brief, system prompt, scoped tools, and *ephemeral*
working memory. The worker does the job, talks to Moko privately when it needs a
fact or a judgment call, and reports back. Moko reviews the report, decides what
(if anything) enters memory, and tells the companion.

`MISSION.md` is the referee. The four safety floors hold across delegation —
this doc says how.

## The one rule that shapes everything: a sub-agent never writes memory

Moko's memory graph has exactly **one writer: Moko.** A sub-agent never mints an
subject, records a fact, writes a thought, or resolves a gap. It reads what it
needs (or asks Moko), works, and returns a **result + report**. Moko consolidates
that report into the graph the same way it extracts knowledge from a
conversation — sourced, timestamped, deduped against what it already knows.

Why this is the keystone:

- **It dissolves the hardest concurrency problem** — multiple writers racing on
  the graph (duplicate subjects, double-resolved gaps, half-merged state). With a
  single writer, N workers run concurrently and the graph's invariants
  (provenance, supersede-not-delete, no silent dupes) are never at risk. We avoid
  the locks-and-races design entirely by *not having* concurrent writers.
- **It keeps the curated mind clean.** A worker's transient chatter — its
  scratch reasoning, dead ends, intermediate files — never pollutes long-term
  memory. Only what Moko judges worth keeping persists, with Moko's own framing.
- **It makes consolidation a deliberate, reviewable act**, like a mini-dream
  after a task, rather than an uncontrolled side effect.

## World ≠ memory

"Doesn't write memory" is precise: it means the **graph**. A sub-agent very much
acts on the **world** — that's the point of delegating it.

- **World state** (filesystem, processes, external services): the worker changes
  it — writes workspace files, installs user-space tools, runs servers, calls
  APIs — gated by the same classifier and consent gate as Moko's own actions.
- **Memory state** (the graph): only Moko writes, on report-back.

## Async tasks, not threads

A sub-agent is a Brain-style loop running as an **asyncio task**, not an OS
thread. Moko is async end to end; concurrency comes from the event loop,
cooperatively, with no locks and no GIL contention. (Hermes uses threads only
because its agent core is *synchronous*; we don't have that constraint, and
threads would add races to solve a problem asyncio already solves more cleanly.)

Threads have essentially no role here:

- A single blocking library call, if one ever forces it → surgical
  `asyncio.to_thread`, not a threaded architecture.
- Isolation for a less-trusted worker → a **subprocess or container**, never a
  thread (threads share memory, so they give no isolation at all).

## Lifecycle

1. **Decide to delegate** — Moko judges the task warrants a worker (size,
   duration, focus). Small things it still does inline.
2. **Brief** — Moko composes the worker's system prompt, the task prompt, the
   scoped toolset, the relevant facts it already holds, and the **capability
   envelope** (what the worker may do without re-asking).
3. **Spawn** — a tracked asyncio task, surfaced in the monitor like the process
   supervisor's background processes.
4. **Work + converse** — the worker runs its own tool loop. When it needs a fact
   Moko holds or a judgment call, it asks Moko over a private channel; Moko
   answers (from the graph or its own reasoning). The companion sees a tidy
   "working on it" surface, not the raw worker chatter.
5. **Report** — the worker returns a structured result: artifacts produced, what
   it did, what it learned, what failed, what it's unsure about.
6. **Consolidate** — Moko reviews the report, persists what's worth remembering
   (sourced to the worker + task), and reports back to the companion.

## Communication

Moko ↔ worker is a private in-process channel (async queues). Much of "ask Moko
what it knows" is really just *reading the graph* — and since the worker never
writes, scoped **read** access is safe.

Open question: read-the-graph-directly (efficient) vs. all-reads-mediated-by-Moko
(cleaner isolation, fully sandboxable). Default lean: Moko hands a curated brief
up front and answers questions on demand; direct scoped reads are an optimization
to add if Q&A round-trips prove chatty.

## Approvals bubble up; consent attaches to the task

A worker can't talk to the companion directly, so a gated world-action **bubbles
up: sub-agent → Moko → companion → back down** — a chain of awaited futures, which
the inline-async approval model composes cleanly (the old park-and-resume model
could not have).

But per-action prompting *through* a worker gets noisy fast — a "build a website"
job might hit ten gated steps. So delegation pushes consent toward a **capability
envelope**: the companion approves the *delegation* plus a scope ("build a site
in the workspace; user-space tools; no `sudo`; no data leaving the machine"), and
the worker runs freely **within** it, only bubbling up actions that exceed the
envelope. Same shape as OpenClaw's "main session trusted, sub-sessions scoped" and
the per-integration allow-list — **consent on a scope, not just a single action.**
The hardline blocklist (`rm -rf ~`, fork bombs, raw-disk writes, pipe-to-shell)
still sits above every envelope and is never grantable.

## Workspace scoping

Each worker gets its own subdir under `~/.moko/work/<task>/`, so concurrent
workers don't collide on files. The shell classifier's workspace bound applies
per worker.

## Safety floors (unchanged)

- **Honesty** — a worker may deflect, never lies; Moko owns and frames what it
  reports to the companion.
- **Consent** — bubbled up, scoped to an envelope, hardline floor above it.
- **Governors** — a worker never spams the companion; it reports to Moko, who
  decides what (and when) reaches them, under the quiet-hours/budget rules.
- **Provenance** — everything a worker's report contributes is sourced +
  timestamped when *Moko* persists it.

## Deferred / open

- Read access: direct scoped reads vs. fully mediated.
- The capability-envelope vocabulary — how scopes are expressed and enforced.
- A concurrency budget (max live workers), like the workflow agent cap.
- Persistence of long runs across a Moko restart (today a worker dies with the
  process and its report is lost — acceptable for v1; revisit for hours-long
  tasks).
- Isolation tier (subprocess/container) for workers running less-trusted work.

## Build order (prerequisites)

1. **Gateway concurrency** — evolve from strict-serial to "main turn + N tracked
   tasks." The single-graph-writer invariant is *preserved for free* by the
   no-memory-writes rule, so this stays a scheduling change, not a locking one.
2. **Sub-agent runtime** — a Brain variant with an injected brief / system prompt
   / scoped tools, **no memory-write tools**, and a structured report contract.
3. **Parent↔child channel + consolidation** — Moko extracts memory from a report.
4. **Capability-envelope consent.**
5. **(Later)** isolation + cross-restart persistence.
