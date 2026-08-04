# SCHEMA.md — Moko's Mind

The memory graph is Moko's reason to exist. This document designs it
*before* code, deliberately, because schema decisions are roughly
forever. It is a design spec, not the migrations themselves — the
numbered `.sql` files in `src/moko/migrations/` are the source of truth
once we start building; this file is the rationale they implement.

Status: **core decisions locked 2026-06-22** (see "Resolved decisions"
at the end); table columns remain refinable until the M1 migrations are
written, which become the source of truth.

---

## Design principles

1. **Everything is time-aware.** Every row knows when it was created,
   and facts/relationships know the period they were *true* — not just
   when Moko learned them. People move, marry, change jobs; the graph
   must hold history, not just the present. **Timestamps are stored in
   UTC** and always *presented/reasoned about* in the companion's
   configured timezone (never a hard-coded one), so travel and DST
   never corrupt the record.
2. **Everything is sourced.** No fact, relationship, or attribute
   enters the graph without provenance: which message, email, web page,
   or inference produced it, and how confident Moko is.
3. **Knowledge and thoughts are separate.** Shareable facts about the
   world live in one place; Moko's private opinions live in another.
   The boundary is structural, so "is this safe to say out loud?" is a
   column, not a judgment call at speak-time.
4. **Soft, not destructive.** Facts are superseded, not deleted —
   `valid_to` over `DELETE`. The graph keeps its
   memory of having changed its mind. **Nothing is ever truly
   forgotten** — thoughts and facts may lose *weight* (prudence/
   confidence decay), but they are not erased.
5. **Values are typed.** Attribute/fact values carry a real type
   (`value_type`), validated on write. The type set is extensible —
   start small, add types as needed — but "everything is a string" is
   not the model.
6. **Sentiment is first-class.** How the speaker *felt* is recorded
   alongside what they said — perceived, time-stamped, with confidence.
7. **The night consolidates.** Cheap/conservative work happens at write
   time; careful, compute-heavy reconciliation is deferred to the
   nightly `dream` process. The schema is shaped to support that split.
8. **IDs are ULIDs.** Sortable, time-bearing, decentralized.

---

## The brain, as a map

The tables aren't an arbitrary pile — they form a mind with distinct
memory systems, the same way a brain does. Knowing which system a table
belongs to is how you decide where a new piece of information lives.

| System | Tables | What it holds |
|--------|--------|---------------|
| **Semantic memory** — knowledge of the world | `subjects`, `subject_aliases`, `attributes`, `observations`, `relationships`, `sources`, `media`, `inference_inputs`, `subject_merges`, `merge_candidates` | The time-aware subject graph: who/what exists, their current properties, the datapoints they accumulate over time, how they connect, where every claim came from, and the artifacts Moko was shown. |
| **Episodic memory** — what happened | `messages` | The dated conversational record, with the speaker's perceived sentiment. |
| **Working memory** — the present moment | `sessions` | The bounded, rotating context fed to the LLM. Ephemeral by design; knowledge is consolidated out before it rotates. |
| **Inner life** — the subjective | `thoughts`, `thought_links` | Moko's private opinions, feelings, concerns, hypotheses — weighted by prudence, never deleted. |
| **Drives** — what to learn and do | `knowledge_gaps`, `commitments` | Open curiosity (questions to answer) and held intentions (things to do or remind). |
| **Self-regulation & audit** | `outreach`, `outreach_decisions`, `actions`, `dreams`, `approvals` | The governors' and consolidator's own memory: what was said unprompted, what was weighed and held off, what was done, what each night reconciled, what was consented to. |

(`app_state` and `schema_migrations` are operational infrastructure — runtime
key/value state and the migration ledger — not memory systems, so they sit
outside this map.)

Two deliberate shapes fall out of this map:

- **"Knowledge" is not a single `facts` table.** It is decomposed into
  `subjects` + `attributes` (current properties) + `observations` (dated
  datapoints in a series) + `relationships` (edges between subjects), each
  sourced and time-bounded. A flat facts table would lose the graph; the
  decomposition is the whole point.
- **The honesty boundary has a structural spine.** Shareable knowledge
  lives in the semantic system; private impressions live in `thoughts`
  with a `shareability` column. The column is load-bearing where a code
  choke point exists — the proactive-share/outreach path only ever
  surfaces a `shareable` thought (it never volunteers a `private`/`on_ask`
  one). For a free-text *reply* there is no discrete speak-gate to hook,
  so the column instead travels as a visible label on every thought
  rendered into Moko's context (the ambient glimpse, `lookup`, `reflect`);
  the `honesty` skill makes honoring it the rule. So "is this safe to
  say?" is a lookup the deliberate surfaces enforce and the reply path is
  guided to respect — structural where it can be, legible everywhere else.

The names are kept deliberately stable — schema decisions are roughly
forever, and these are already domain-accurate. New information gets a
home by asking which system above it belongs to, not by adding a table.

---

## Core tables

### `subjects`
The nodes. People, places, organizations, objects, events, topics,
even Moko itself and the companion.

| column        | type    | notes |
|---------------|---------|-------|
| `id`          | ULID PK | |
| `kind`        | text    | `person` \| `place` \| `org` \| `object` \| `event` \| `topic` \| `pet` \| `self` … (extensible enum) |
| `name`        | text    | canonical display name |
| `created_at`  | ts      | when Moko first recorded it |
| `confidence`  | real    | 0–1; how sure Moko is this subject is real/distinct |
| `notes`       | text    | freeform, non-graph scratch |
| `merged_into` | ULID FK?| null = a live node; set = this subject was folded into another (non-destructive merge — see *Merging*) |

- **Aliases** get their own table (`subject_aliases`: `subject_id`,
  `alias`, `source_id`) so "Maya" / "Maya Tanaka" / "my sister"
  resolve to one node.
- **Merging:** subject resolution will create duplicates; merging is
  **non-destructive** — the folded node is kept and tagged with
  `merged_into` (pointing at the canonical subject) rather than deleted,
  so it can't re-attach new facts and recall won't surface it, but the
  history survives. An `subject_merges` log (`from_id`, `into_id`, `at`,
  `by` ∈ write/dream/user, `reason`) records each merge so they're
  auditable and (in principle) reversible.
- **Merge candidates (the hybrid resolver):** at write time Moko only
  auto-merges on *strong* signals (exact alias, unambiguous match).
  Anything weaker is written as a **distinct** node plus a row in
  `merge_candidates` (`a_id`, `b_id`, `score`, `evidence`, `status` ∈
  open/confirmed/rejected, `created_at`). The nightly `dream` process
  reviews these with full graph context and more reasoning, confirms or
  rejects them, splits any bad merges it finds, and — when genuinely
  ambiguous — opens a `verification`/`dangling_ref` knowledge-gap so
  Moko can ask. **Be safe at write time, be smart at night.** This is
  how we get neither wrong merges nor permanent fragmentation.

### `attributes`
Properties of a single subject. A friend's birthday, a home address, a
pet's name, a café's opening hours.

| column       | type    | notes |
|--------------|---------|-------|
| `id`         | ULID PK | |
| `subject_id`  | ULID FK | the subject |
| `key`        | text    | `birthday`, `address`, `email`, `dietary`… |
| `value`      | text    | serialized value; parsed/validated per `value_type` |
| `value_type` | text    | authoritative type, **validated on write**: `string` \| `date` \| `datetime` \| `number` \| `bool` \| `json` … (extensible) |
| `valid_from` | ts?     | when it became true (nullable = unknown/always) |
| `valid_to`   | ts?     | when it stopped being true (null = still true) |
| `source_id`  | ULID FK | provenance |
| `confidence` | real    | 0–1 |
| `created_at` | ts      | |

### `observations`
Time-anchored datapoints — the **append-only twin of `attributes`**. An
attribute is the current value of a property ("their bedtime"); an observation
is one reading in a *series* ("slept 7.2h last night", "ran 5k today", a
cancelled plan), true at its `observed_at` instant forever. A new reading never
supersedes the last, so the series accumulates instead of overwriting (the
attribute footgun) or piling up date-stamped keys. The date lives in
`observed_at`, **never** in the `key`. This is the substrate for pattern-noticing
over time ("you've cancelled three runs this month").

| column       | type    | notes |
|--------------|---------|-------|
| `id`         | ULID PK | |
| `subject_id` | ULID FK | the subject (usually the companion) |
| `key`        | text    | stable series name: `sleep_hours`, `weight_kg`, `run_cancelled` — never date-stamped |
| `value`      | text    | serialized; parsed/validated per `value_type` |
| `value_type` | text    | validated on write, same set as `attributes` |
| `observed_at`| ts      | UTC instant the datum is true (event time) — distinct from `created_at` |
| `source_id`  | ULID FK | provenance |
| `created_at` | ts      | when Moko recorded it |

`UNIQUE (subject_id, key, observed_at)` — restating a reading updates it in
place, so a `COUNT`/`AVG` over the series never double-counts. No `valid_to` (a
past reading never stops having been true), no FTS row (a series is read by
`(subject, key)` through the subject, not by free-text recall).

**Attribute or observation?** The decisive test: *would a newer value REPLACE
the old one, or SIT BESIDE it?* Replace → attribute (state). Sit beside →
observation (series). "Current X" is the latest observation, never *also* a
parallel attribute. *Open follow-on:* a dream-side canonicalizer for series keys
— the analog of subject `merge_candidates` — so `sleep_hours`/`hours_slept`
can't fragment one series and silently corrupt an aggregate; until it lands, the
`remembering` skill's reuse-exact-key rule is the guard.

### `relationships`
The edges. Directed, typed, time-bounded.

| column       | type    | notes |
|--------------|---------|-------|
| `id`         | ULID PK | |
| `from_id`    | ULID FK | subject subject |
| `to_id`      | ULID FK | object subject |
| `type`       | text    | `married_to`, `parent_of`, `works_at`, `friend_of`, `lives_in`, `owns`, `member_of`… |
| `valid_from` | ts?     | |
| `valid_to`   | ts?     | null = current |
| `directed`   | bool    | symmetric (`friend_of`) vs directed (`parent_of`) |
| `source_id`  | ULID FK | provenance |
| `confidence` | real    | |
| `created_at` | ts      | |

Symmetric relationships are stored once with `directed = false` and
queried both ways, rather than duplicated.

### `sources`
Provenance. The thing every fact points back to.

| column       | type    | notes |
|--------------|---------|-------|
| `id`         | ULID PK | |
| `kind`       | text    | `message` \| `email` \| `web` \| `file` \| `calendar` \| `inference` \| `user_stated` |
| `ref`        | text    | message id / URL / file path / thread id |
| `channel`    | text?   | where it came from (telegram, gmail…) |
| `excerpt`    | text?   | the snippet that grounded the fact |
| `observed_at`| ts      | when the source was created/seen |
| `created_at` | ts      | |

An `inference` source is *meant* to point at the facts it was derived *from*
(`inference_inputs`: `inference_source_id`, `input_ref`) so Moko
can explain "I think X because Y and Z."

> **Status — planned, not yet wired (2026-06-30).** `inference` sources are written
> (they're the largest provenance category), but the `inference_inputs` chain that
> backs the explanation has no writer and is empty. The "because Y and Z" capability
> doesn't exist yet — wire the basis on the inference-write path, or read this as design
> intent until then.

### `messages`
The conversational record — what was said, by whom, when, on what
channel. The substrate curiosity and proactivity reason over.

| column        | type    | notes |
|---------------|---------|-------|
| `id`          | ULID PK | |
| `channel`     | text    | telegram, cli… |
| `speaker_id`  | ULID FK | subject who said it (companion, Moko, a third party) |
| `direction`   | text    | `in` \| `out` |
| `body`        | text    | |
| `sent_at`     | ts      | UTC; rendered in the configured tz |
| `sentiment`   | text?   | perceived emotion label of the **speaker** (`happy`, `stressed`, `flat`, `excited`, `upset`…) |
| `valence`     | real?   | -1..1, perceived pleasantness of the speaker's state |
| `arousal`     | real?   | 0..1, perceived intensity/energy (optional) |
| `sentiment_confidence` | real? | 0..1; sentiment is a *hypothesis*, marked as such |
| `session_id`  | ULID FK?| the working-memory session this message belongs to |
| `meta`        | json    | reply-to, attachments, etc. |

Sentiment here is about **the speaker**, perceived by Moko — distinct
from `thoughts.valence`, which is how *Moko* feels. The same perceived-
feeling fields can attach to non-message observations via the
`observations` shape if needed later; messages cover the common case.
Time-stamped sentiment makes emotional **trends** queryable (the basis
for well-being check-ins and pattern notices).

**Valence is interpreted felt-state, not lexical polarity** — affectionate
swearing among friends is high-arousal *positive*, not negative. The
well-being trend (`store.recent_affect` → `Brain._should_check_in`) reads
all three axes together: it counts only calm-to-flat reads (a keyed-up
negative is anger/agitation/banter, not the low mood a check-in is for)
and **weights each by `sentiment_confidence`**, so an unsure read can't
drive a governed outreach on its own. `sentiment_confidence` is therefore
load-bearing, not write-only — keep writing it.

**Known limitation — the valence column is overloaded** (calibration audit,
2026-06-29). One scalar serves two masters: the *momentary* affect of a
single message and the *sustained* well-being/room-reading signal two
systems threshold. That's fine until we want to record "the words are
hostile but the felt state is warm" as anything other than a single
interpreted number. Empirically the column also under-uses its negative
half (Moko's recorded reads cluster +0.5..+0.9 while arousal uses its full
range), partly a guidance bias now addressed in the `remembering`/
`inner-life` skills. If true surface-vs-intent separation is ever needed,
the durable move is a **second axis** (surface valence the trend ignores)
or a **governor-honored `play`/`banter` flag** — a schema+consumer change,
*not* a prompt tweak. **The character sheet is built by aggregation, not by
adding more per-message axes:** dispositional traits ("communicates at high
arousal", "humor is deadpan-aggressive", baseline valence) are *derived*
over time from the state already captured — and the richest untapped signal
is the free-text `sentiment` label, which deserves to become queryable
before any new scalar column is minted.

### `media` *(M10)*
Binary artifacts — images, voice notes, documents — that ride on a
message or that Moko generates. The **bytes live on disk** under
`~/.moko/media/`, content-addressed by `sha256` (so the same photo sent
twice shares one blob); this row is the **sourced, time-stamped record**.
A media artifact is a fact in the provenance sense, so it has a
`source_id` like everything else — "every fact is sourced" holds for what
Moko is *shown*, not just what it's told.

| column       | type    | notes |
|--------------|---------|-------|
| `id`         | ULID PK | |
| `source_id`  | ULID FK | provenance (mandatory). Inbound = a `message` source; what Moko later *infers* from it is a separate `vision`/`inference` source. |
| `message_id` | ULID FK?| the message it arrived on (null = generated / outbound) |
| `kind`       | text    | `image` \| `audio` \| `video` \| `document` |
| `mime`       | text?   | `image/jpeg`, `audio/ogg`, `application/pdf`… |
| `sha256`     | text    | content hash — dedup + integrity |
| `bytes`      | int     | size on disk |
| `path`       | text    | stored name under `~/.moko/media/` (relative → portable) |
| `filename`   | text?   | the sender's original filename, for display/recall |
| `caption`    | text?   | the sender's caption, if any |
| `description`| text?   | *Moko's* observation of what an image showed — the durable "gist" it keeps after looking (vision); FTS-indexed, so `recall` finds it after the session rotates. Distinct from `caption` (the sender's words). |
| `direction`  | text    | `in` (received) \| `out` (sent/generated) |
| `created_at` | ts      | UTC (when received/recorded) |
| `taken_at`   | ts?     | EXIF capture time, distinct from `created_at`; null when unknown |

The blob layer (`store/media.py`) writes content-addressed, owner-only
(`0600`) bytes; the store row carries the provenance. Vision/voice
(reading an image, transcribing a voice note) and generation (TTS, a
self-portrait) ride on this table — what they *understand* or *produce*
attaches as further sourced facts. See ROADMAP M10.

> **Deprecated column.** `world_analyzed` (added in migration 0016 for vision
> back-analysis of generated images) is inert: that flow was retired when the world
> model moved to brain-authoring (commit `7193a79`). Its index `idx_media_world` was
> dropped in migration 0022; the column is left in place (soft schema) but unused.

### `sessions`
A stretch of **working memory** — the bounded conversational context fed
to the LLM. Durable knowledge lives in the graph; a session is ephemeral
and rotates to stay small, fresh, and cheap (see MISSION § Working memory
and sessions). Messages carry a `session_id`.

| column          | type    | notes |
|-----------------|---------|-------|
| `id`            | ULID PK | |
| `started_at`    | ts      | UTC |
| `ended_at`      | ts?     | null = the active session |
| `started_reason`| text    | `first` \| `dream` \| `user` \| `size` \| `idle` |
| `ended_reason`  | text?   | why it rotated |
| `carryover`     | text?   | optional short hand-off note to bridge a mid-conversation rotation |
| `token_estimate`| int     | rough running size, for the size-based rotation trigger |

Rotation is policy, implemented with the agent loop (M2): a session is
rotated **only after its content is consolidated into the graph**, so the
verbatim transcript is dropped but knowledge never is. The dream process
(M6) ends the active session as part of consolidation.

### `thoughts`
Moko's private inner life. Impressions, opinions, concerns,
hypotheses, predictions, curiosities, feelings.

| column         | type    | notes |
|----------------|---------|-------|
| `id`           | ULID PK | |
| `kind`         | text    | `opinion` \| `concern` \| `hypothesis` \| `prediction` \| `curiosity` \| `feeling` \| `note` |
| `body`         | text    | the thought itself |
| `valence`      | real?   | -1..1, how Moko *feels* about the subject (for opinions/feelings). **Live:** set via the `remember` tool; high `\|valence\|` raises a thought's initial `stability` (flashbulb → decays slower). |
| `shareability` | text    | `private` (default) \| `on_ask` \| `shareable` |
| `prudence`     | real    | 0..1 current weight Moko gives this thought when reasoning/volunteering. **Decays, but never to deletion.** Low prudence ≠ forgotten — still fully queryable. |
| `stability`    | real    | resistance to decay; rises with corroboration, drops on contradiction (see model below) |
| `created_at`   | ts      | UTC |
| `last_reinforced_at` | ts? | last time evidence corroborated/contradicted it |
| `last_recompute_at` | ts? | when `dream` last re-weighted it |
| `reconsider_at`| ts?     | optional hint: when this impression is worth revisiting |

- **What a thought is about:** `thought_links` (`thought_id`, `target_kind`,
  `target_id`). **As built:** the kinds emitted are `subject` (the read edge —
  `lookup`/`reflect` walk it) and `source` (write-only provenance, since `thoughts`
  has no `source_id` of its own). The wider vocabulary `fact`/`relationship`/`message`/
  `gap` and the many-to-many shape are reserved/aspirational — today every thought
  links exactly one subject. The join table is kept for that future many-to-many.
- `shareability` is the structural half of the honesty boundary:
  `private`/`on_ask` thoughts are never volunteered; deflection logic
  reads this column.

**Prudence decay model (evidence-driven, not clock-driven).** Modeled on
how a human mind actually works — Ebbinghaus forgetting *plus* spaced
repetition, reconsolidation, and emotional salience:

> **Status — designed vs. shipped (2026-06-30).** What ships today is the nightly
> *multiplicative tick decay* — `prudence = max(floor, prudence·(1 − rate·(1 − stability)))`,
> recomputed by `dream`, with `stability` set once at write time from `|valence|`. The
> full evidence-driven loop below (corroboration raising `stability`, contradiction
> dropping it) is the *target*, not yet built: `last_reinforced_at` and `reconsider_at`
> are reserved for it and currently unwritten, and `last_recompute_at` is written but not
> yet read. Read the bullets below as the design.

- `prudence = stability × decay(time since last_reinforced_at)`.
- **Corroboration raises `stability`** (the curve flattens — an old
  belief confirmed often is rock-solid; spaced-repetition).
- **Contradiction sharply drops prudence/stability** (the strongest
  signal) — but the thought is *never deleted*.
- **High salience/valence → higher base stability** (flashbulb effect).
- Recomputed **nightly by `dream`**, which sees the day's corroborations
  and contradictions.
- **Knobs are per `kind`:** a `concern` decays slower than a passing
  `note`; a behavior-confirmed `opinion` is sticky. Half-life +
  reinforcement/contradiction step sizes live in config.
- This is deliberately *unlike* a human in one way: low prudence means
  *weighted less*, never *lost*. Human-like judgment, machine retention.

(Objective **facts** decay differently — via `confidence` + category
staleness, e.g. an address re-confirms periodically, a birthday never
goes stale. Prudence is for subjective thoughts only.)

**Inner-life continuity (shipped, M7).** Thoughts are no longer write-only.
Three read paths close the loop, so opinions and mood carry across turns and
sessions: (1) a `reflect` tool lets Moko query its own past thoughts on demand —
`reflect("Biscuit")` recalls what it thought about a thing (FTS over thought
bodies), `reflect()` recalls what's been on its mind lately; (2) `lookup` on an
subject surfaces the thoughts *linked* to it, alongside the facts; and (3) a
short, hard-capped glimpse of Moko's most recent thoughts rides in the volatile
tail each turn, so its mood is present without having to ask. Linking is done at
write time: the `remember` tool's thoughts take an `about` field, resolved to an
existing subject (never minting one) and written to `thought_links` — which
`merge_subjects` now re-points so the inner life follows a fold. Faded (low-
prudence) thoughts still surface via `reflect`/`lookup` (ranked lower); only the
ambient glimpse filters them out, so nothing is forgotten.

**Autonomous inner life (idle reflection).** The inner life is no longer only
*reactive* (reply-driven). On a quiet heartbeat — outside the sleep window, the
chat settled, a cooldown elapsed, and only when there's fresh material —
`Brain.reflect_idle` lets Moko form private thoughts and sharpen curiosities on
its own, **without ever messaging the companion** (if something's worth saying it
raises a gap; the governed outreach path surfaces it later). It's gated like
outreach so it's a no-op on most ticks. See `INNER-LIFE.md`. The night's `dream`
also now closes gaps the graph has come to answer, so Moko stops asking what it
already knows.

### `knowledge_gaps`
Curiosity, made queryable. The engine that drives Moko's questions.

| column         | type    | notes |
|----------------|---------|-------|
| `id`           | ULID PK | |
| `about_id`     | ULID FK?| subject the gap concerns (nullable for open-ended) |
| `description`  | text    | "which colleague is 'my manager'?", "Maya's birthday?" |
| `kind`         | text    | `missing_attr` \| `unconfirmed_rel` \| `dangling_ref` \| `stale_fact` \| `causal` (why?) \| `verification` (is that true? / contradiction) \| `open_curiosity` |
| `resolution_path` | text | `research` (world can settle it) \| `ask` (only the companion knows) \| `either`. Drives "try to answer it myself before asking." |
| `salience`     | real    | 0–1; interest × usefulness × relationship value × age |
| `status`       | text    | live: `open` \| `researching` \| `resolved` \| `abandoned`. `queued`/`asked` are reserved for the proactivity scheduler — defined but not yet written. |
| `opened_at`    | ts      | UTC |
| `last_attempt_at` | ts?  | last research attempt — for retry/backoff |
| `last_asked_at`| ts?     | for backoff — don't re-ask too soon |
| `resolved_by`  | ULID FK?| the source/message that closed it |

`resolution_path` encodes Moko's "answer it myself first" instinct:
`research` gaps go to the tool layer (web/graph cross-check) before ever
spending an outreach to the companion; `ask` gaps are genuinely personal.
A `verification` gap is born whenever a new fact contradicts an existing
one (or arrives surprising/low-confidence) — Moko investigates rather
than silently overwriting. Salience decays/grows over time; the
proactivity governor pulls the top open gaps when it has budget to
spend.

### `commitments`
Things Moko has agreed to *do* or *remind* the companion about — the
helpful counterpart to `knowledge_gaps` (things to *learn*). The
heartbeat surfaces these when they fall due.

| column         | type    | notes |
|----------------|---------|-------|
| `id`           | ULID PK | |
| `description`  | text    | "remind Andrew about the dentist", "send Mia the photos" |
| `kind`         | text    | `reminder` \| `task` \| `followup` |
| `about_id`     | ULID FK?| subject it concerns |
| `due_at`       | ts?     | UTC; when it should surface (null = someday/no fixed time) |
| `status`       | text    | `open` \| `done` \| `cancelled` |
| `source_id`    | ULID FK?| where the request came from |
| `created_at`   | ts      | UTC |
| `completed_at` | ts?     | |

A genuinely helpful agent has to *hold* what it was asked to do — without
this, "remind me Friday" is forgotten. The heartbeat (M3) checks for
`open` commitments whose `due_at` has passed and surfaces them through
proactive outreach (M5).

### `outreach`
The proactivity governor's memory — so "moderate, never spammy" is
enforced against real history, not hope.

| column        | type    | notes |
|---------------|---------|-------|
| `id`          | ULID PK | |
| `channel`     | text    | |
| `kind`        | text    | as built: `curiosity` \| `commitment` \| `wellbeing` \| `reconnect`. (The original draft enum — `question`/`checkin`/`surface`/`notice`/`consent_request`/`urgent` — was superseded by the candidate vocabulary the heartbeat actually emits; kept here as the record of intent.) |
| `gap_id`      | ULID FK?| if it was curiosity-driven |
| `counts_to_budget` | bool | curiosity counts; **necessity is exempt** (`commitment`/`wellbeing` and any future urgent kind). `reconnect` counts. |
| `sent_at`     | ts      | UTC; sleep window checked in configured tz |
| `engaged`     | bool?   | did the companion respond? (feeds backoff) |

Daily budget = count over `sent_at` in the rolling window; the dream
window is checked against `sent_at` rendered in the **configured
timezone**.

### `dreams`
The dream journal — what each night's `dream` process consolidated, so
it's auditable.

| column        | type    | notes |
|---------------|---------|-------|
| `id`          | ULID PK | |
| `started_at`  | ts      | UTC |
| `ended_at`    | ts?     | |
| `summary`     | text    | human-readable: what was reconciled/recomputed |
| `stats`       | json    | merges confirmed/rejected, thoughts re-weighted, gaps re-scored, upgrades applied/deferred |
| `wonders`     | text?   | what Moko is most curious about going into tomorrow |

### `approvals`
The consent gate's audit trail: every sensitive action parked for the
companion's approval and how it resolved — a safety-floor record of what was
asked and consented to. *(Original intent, **deferred**: pre-approving
self/system upgrades during the day so `dream` could execute them at night.
That upgrade machinery isn't built yet; today the table records the day-to-day
consent-gate decisions.)*

| column        | type    | notes |
|---------------|---------|-------|
| `id`          | ULID PK | |
| `action`      | text    | the proposed action — a JSON payload (tool + args + description) |
| `kind`        | text    | the action's category (the consent boundary it crossed) |
| `status`      | text    | `pending` → `executed` \| `denied` \| `superseded` \| `expired` |
| `requested_at`| ts      | UTC |
| `decided_at`  | ts?     | |
| `executed_at` | ts?     | when it ran |

---

## Cross-cutting concerns

- **Indexes:** by `subject_id` on attributes/relationships; by
  `valid_to IS NULL` for "current" queries; FTS5 virtual table over
  `messages.body`, `attributes.value`, `subjects.name` (and aliases),
  `thoughts.body`, and `media.description` for fast recall.
  (`observations` are deliberately *not* indexed — a series is read by
  `(subject, key)`, not by free text.)
- **Context assembly:** a `graph.slice(context)` function returns the
  relevant sub-graph for a given conversation (the subjects mentioned +
  one hop of relationships + their current attributes + recent
  messages), to inject into the LLM prompt. The graph is useless if it
  can't be *retrieved* cheaply.
- **Confidence & decay:** facts carry confidence; low-confidence facts
  are candidates for curiosity ("is this still right?"). Some
  attributes are inherently stale-able (job, address) and get periodic
  re-confirmation gaps.
- **Self & companion as subjects:** Moko (`kind=self`) and the companion
  are nodes in the same graph, so thoughts/relationships about *them*
  use the same machinery.

## Resolved decisions (locked 2026-06-22)

1. **Typed values, not all-text.** Values carry an authoritative
   `value_type`, validated on write; the type set is extensible. Typing
   buys correctness and good queries; we can always add types.
2. **Confidence is a number (0–1), not an enum.** A continuous scale
   gives finer ranking for curiosity/verification than `certain /
   likely / guessed`. Same for sentiment confidence.
3. **Thoughts never expire.** They lose *weight* via `prudence` decay
   (recomputed by `dream`) and may become less prudent to act on, but
   nothing is ever truly forgotten. No auto-deletion.
4. **Subject resolution is hybrid (write-time + dream).** Strong matches
   merge immediately; weak ones are stored as distinct nodes plus a
   `merge_candidates` row, reconciled carefully at night by `dream` with
   full context — splitting bad merges, consolidating fragments, and
   asking the companion only when truly ambiguous. This is the answer to
   "would dream resolve wrong-merge vs. fragmentation?": **yes — that's
   exactly what the night is for.**

With these locked, the schema moves from *draft* to *ready to
implement* once Milestone 1 opens; the migrations will be its source of
truth.
