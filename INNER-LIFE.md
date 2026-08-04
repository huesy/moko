# INNER-LIFE.md — Autonomy spec (idle reflection · stale-gap resolution · thought valence)

All three pieces are implemented against this spec. `MISSION.md` is the referee;
where this and the mission disagree, the mission wins. Where the code lives, vs.
the design below:

- **Thought valence** — `valence` on the `remember` tool's thoughts →
  `_apply` → `add_thought`; `_stability_from_valence` gives strongly-felt
  thoughts higher initial stability (flashbulb), so they decay slower. Skill
  updated. (`brain.py`, `memory_tools.py`, `inner-life` skill.)
- **Stale-gap resolution** — `resolve_satisfied_gaps` now also closes a
  `missing_attr` gap once its subject gains a new key-matching attribute;
  the dream's `_resolve_answered_gaps` (LLM) closes the fuzzy / no-`about_id`
  ones. (`store/graph.py`, `agent/dream.py`.)
- **Idle reflection** — `Brain.reflect_idle()` gated by fuses (not in the dream
  window, chat settled, an interval since `reflect_last_at`), forms one or two
  private thoughts (with valence) + gaps, **never sends**, wired into the
  gateway's idle branch. Reflection has its **own, shorter settle (~10 min)** —
  it's private, so it doesn't wait for the outreach "conversation is over" bar
  (30 min). With *fresh* material it reflects within ~10 min; with nothing new it
  keeps **musing** on what it knows (~30 min base), backing off toward a 6 h cap
  when a muse turns up empty (`reflect_dry`) — alive during quiet stretches
  without looping on a static graph. The cadence lives in `agent/rhythms.py`
  (the one source of truth, shared with the monitor's rhythm bar).
- **Observability** — the brain publishes a `reflection` event (mode + whether
  anything landed) and an `outreach_consult` event (act/wait + the brain's
  reasoning), so a *decision to wait* is visible in the monitor instead of
  looking like nothing happened. The monitor renders both (and tooltips explain
  the thought card's valence/date). (`brain.py`, `apps/monitor/`.)

The design below is retained as the rationale.

## Why

Moko's inner life is real but **reactive**. Today:

- **Thoughts only form during reply turns** (the `remember` tool). Between
  conversations Moko does no cognition — the observer feed shows only heartbeat
  ticks. That contradicts the mission's "subject with an inner life… forms private
  opinions… not a request/response loop."
- **`thoughts.valence` is a dead column** — never written (no field on the
  `remember` tool) and never read (decay uses `stability` only). SCHEMA intends
  it for the flashbulb effect (emotionally charged thoughts persist).
- **Answered `missing_attr` gaps don't close.** `resolve_satisfied_gaps` only
  handles `dangling_ref`. So Moko can re-ask what it already knows ("what was in
  the package?" after you said *HeroQuest*) — which reads as not-sharp.

This spec makes the inner life **autonomous and self-tending**, within the
existing governors and **without new spam** (reflection is private by default —
it never sends).

Three pieces, build in this order (smallest/enabling first):

1. **Thought valence** — record how Moko feels; use it in decay.
2. **Stale-gap resolution** — close gaps once the graph answers them.
3. **Idle reflection** — form private thoughts on quiet heartbeats.

---

## 1. Thought valence

**Goal:** `thoughts.valence` carries how *Moko* feels about a thought's subject
(−1..1), and emotionally charged thoughts decay slower.

**Changes**

- `memory_tools.REMEMBER_TOOL` — add `valence` (number, −1..1, "how *you* feel
  about this — fond/positive > 0, troubled/negative < 0; omit for neutral notes")
  to the `thoughts` item schema. Add the same field to the idle-reflection
  thought tool (piece 3).
- `brain._apply` thoughts loop — pass `valence=_as_float(thought.get("valence"))`
  into `store.add_thought`.
- `store.add_thought` — already accepts `valence`; additionally derive a higher
  initial **stability** for strongly-felt thoughts (the flashbulb effect):
  `stability = clamp(base_stability + K * abs(valence), 0, 1)` when valence is
  given (base/`K` per-kind knobs in config; keep current default when valence is
  None). Decay already slows with stability (`prudence *= 1 − rate·(1−stability)`),
  so no change to `decay_thoughts` is required — valence feeds it through
  stability at write time. (Optionally let the dream re-derive stability if a
  thought's valence is later set/changed; not required for v1.)
- `inner-life` skill — instruct Moko to set `valence` on feelings/opinions (not
  on neutral notes).

**Tests**

- A thought recorded with valence 0.9 gets higher stability than a neutral note,
  and after `decay_thoughts` retains more prudence.
- `valence` round-trips through the `remember` tool → `add_thought`.

**Non-goals:** don't touch *message* sentiment (a separate, working axis — the
speaker's perceived mood, used by well-being check-ins).

---

## 2. Stale-gap resolution

**Goal:** a gap closes once Moko actually knows the answer, so it never
unprompted-asks something already told.

**Current:** `store.resolve_satisfied_gaps()` closes a `dangling_ref` gap when
its `about_id` subject gains any facts. Insufficient for `missing_attr` and
`about_id`-less gaps (e.g. "what was in the package?").

**Design — structural fast-path + LLM backstop:**

- **Structural (cheap, write-time):** extend `resolve_satisfied_gaps` so a
  `missing_attr` gap whose `about_id` subject has gained an attribute (or edge)
  whose key/value keyword-overlaps the gap description is marked `resolved`
  (stamp `resolved_by`). Conservative: only on a clear keyword hit, so a
  still-open question is never falsely closed. Runs each turn after extraction
  (where `resolve_satisfied_gaps` already runs).
- **LLM backstop (the fuzzy / `about_id`-less ones):** a dream pass
  `_resolve_answered_gaps` — give the model the open gaps plus a compact graph
  slice and ask "which of these does the graph now answer?" → resolve those.
  Mirrors the existing `_dedup_gaps`. Idle reflection (piece 3) may also close
  an obvious one it notices. Conservative default: leave open when unsure.

**Tests**

- A `missing_attr` gap "Biscuit's age" resolves once a `Biscuit.age` attribute
  lands; an unrelated gap stays open.
- The dream's `_resolve_answered_gaps` closes an `about_id`-less gap the graph
  now answers, and leaves a genuinely open one.

**Watch:** never resolve a gap on a weak/ambiguous match — a falsely-closed gap
is worse than a lingering one (Moko would silently stop being curious). Log what
gets auto-resolved (no silent caps).

---

## 3. Idle reflection — the autonomous inner life

**Goal:** on quiet heartbeats, Moko *thinks* — forms private thoughts, notices
connections, sharpens curiosities — without sending anything. The observer feed
then shows genuine autonomous cognition. **Private by default; reflection never
produces outreach.**

### Where it hooks

`gateway._on_tick`, the idle branch (when `consider_outreach` returns None).
Today that branch does `research_gap` if a research gap exists. Add a reflection
step alongside it: `Brain.reflect_idle(now)`.

### Fuses (deterministic, code — same philosophy as outreach)

Reflection runs only when:

- **Not in the sleep window.** The night is for `dream` (heavy consolidation);
  daytime reflection is light and additive. They must not overlap.
- **Chat has settled** (≥ the settle floor, reuse `_SETTLE_HOURS`, or a touch
  longer) — don't reflect mid-conversation.
- **A reflection cooldown has elapsed** — a persisted `reflect_at` in `app_state`
  (exactly like `outreach_reconsider_at`), so reflection is **not an LLM call
  every 30 s**. Default cadence ~20–30 min; back off when nothing new is
  happening. This is the cost control — guard it hard.
- **There is fresh material** since the last reflection (new messages, new
  facts/thoughts, or open curiosities). No new material → skip the LLM entirely,
  bump `reflect_at`, do nothing. Idle-with-nothing-new must cost zero tokens.

### What it does (one LLM call, a focused tool loop)

Assemble a compact context — recent messages, the subjects/facts touched
recently, Moko's own recent thoughts (`recent_thoughts`), and the top open
curiosities — and let Moko, *as itself*:

- form 0–N **private thoughts** (opinions, feelings, hypotheses, connections),
  **with `valence`** (piece 1), linked to what they're `about`. Reuse the
  thought-recording path; a `reflect_thoughts` terminal tool (thoughts-only
  subset of `remember`) keeps it scoped.
- optionally **sharpen or raise a `knowledge_gap`** (a new curiosity, or
  promote an `ask`/`either` gap to `research` when the world could settle it) —
  feeding the existing research path, which answers it on a later idle tick.
- optionally **close an obviously-answered gap** (overlaps piece 2).

It does **not** reach out. If reflection decides something is worth saying to the
companion, it raises/sharpens a gap or commitment; the **outreach path** (already
governed) surfaces it later. Keep the separation: reflection = inner, governed
outreach = outer.

### Cost & cadence

- `reflect_at` persisted, bounded like `_set_reconsider` (floor ~10 min, default
  ~25 min, cap a few hours). Set it on every reflection (acted or skipped).
- One LLM call per reflection at most. With the cooldown + fresh-material gate,
  that's a handful of calls/hour during active days, zero when idle.
- 30 s heartbeat unaffected: most ticks short-circuit on the fuses with pure DB
  reads (as outreach already does).

### Observability

- Reflection thoughts already publish through `store.add_thought` → the events
  bus → the observer feed (no extra wiring). 
- Emit a `reflection` marker event (start/end, # thoughts formed) so the feed
  shows the autonomous beat distinctly from a reply turn.
- Log the reflection's tool calls to `llm.jsonl` (as the reply loop does), so
  it's auditable — mirror the `outreach_consult` logging just added.

### Reflection vs. dream (keep them distinct)

| | **Idle reflection** | **Dream** |
|---|---|---|
| when | daytime, quiet beats | nightly, sleep window |
| weight | light, one call | heavy, multi-pass |
| effect | **additive** — new thoughts/curiosities | **consolidative** — merges, decay, reconciliation, session end |
| destructive? | never | non-destructive but reorganizes |
| sends? | never | never |

Reflection gathers loosely through the day; the dream still makes it coherent at
night. Reflection must never do dream work (no merges/decay) and the dream still
owns prudence decay and gap dedup.

### Tests

- Fuses: no reflection in the sleep window; none mid-chat; none before
  `reflect_at`; none when there's no fresh material (assert **no LLM call**).
- A reflection with fresh material forms ≥1 thought (with valence) and sets
  `reflect_at`.
- Reflection never emits outreach (no `outreach` row, no send).
- Injectable clock + faked LLM at the seam, like the outreach-consult tests.

---

## Safety / governor notes

- **Reflection is private** — it writes thoughts/facts (sourced) and gaps only,
  never sensitive actions, never sends. So it's outside the outreach *budget*,
  but it respects the **sleep window** and a hard **cost cooldown**.
- **Prompt injection:** if reflection promotes a gap to `research`, the answer
  comes via the existing gated web tools and is recorded as `source_kind="web"`
  with provenance — same untrusted-content posture as today. Reflection itself
  reads only already-trusted local context.
- **No silent caps:** log auto-resolved gaps and skipped reflections (why), so
  the behavior is auditable in `llm.jsonl` / the feed.

## Open questions

- Reflection cadence — fixed cooldown, or adaptive to conversation intensity?
  (Start fixed; tune by observation.)
- How much context to feed reflection (cost vs. quality) — start with the
  outreach-consult context budget and adjust.
- Should a strong reflection ever shorten the outreach `reconsider_at` (so a
  fresh, salient curiosity can surface sooner)? Defer; keep the paths decoupled
  in v1.

## Roadmap / checklist placement

Folds into **M8 (Growth)** or a small **M5.1 (Autonomous inner life)** — decide
when building. Update `ROADMAP.md` + `CHECKLIST.md` then, and refresh
`SCHEMA.md` § *thoughts* (valence now live; flashbulb stability) and the
*knowledge_gaps* note (auto-resolution).

---

## Addendum — affect calibration (2026-06-29)

Triggered by a 20-min provocation thread: the companion tried to upset Moko and
every read stayed pinned at +0.8/+0.9 on both axes. Audit of the whole store
found a **systemic positivity clamp**: across all history, speaker valence
averaged +0.67 (min −0.20 ever, zero clearly-negative reads) and Moko's own
thought valence averaged +0.69 (exactly one read below zero, at −0.10) — while
the **arousal** axis used its full 0.2–0.9 range. Same model, same schema: the
*pleasant↔unpleasant* dimension is clamped, the energy dimension isn't. The
behaviour itself was correct (Moko refused every inappropriate demand, was never
coerced by "I'll turn you off") — the defect is calibration and texture, not
judgement.

**Shipped (this addendum's changes):**

1. **Well-being trend hardened** — `recent_valence` → `recent_affect` (now returns
   valence + arousal + confidence). `Brain._should_check_in` counts only
   calm-to-flat reads (arousal < `WELLBEING_AROUSAL_CEILING`) and takes a
   **confidence-weighted** mean over a confidence-weighted sample mass. This makes
   `sentiment_confidence` load-bearing (it was write-only) and stops a keyed-up
   negative (anger/banter) or an unsure read from firing a governed check-in.
   Tests in `test_brain.py`.
2. **`remembering` skill** — sentiment is never dropped on a terse/blunt/hostile
   message; valence is the *interpreted felt-state* (playful swearing is positive),
   and `confidence` drops when words and feeling genuinely conflict.
3. **`inner-life` skill** — licenses the full range *honestly* (feelings may dip;
   don't manufacture hurt at play either), adds clear-eyed appraisal `note`s, and a
   **safety floor: mood colours tone, never judgement — a stung feeling is never a
   reason to do what you'd refuse** (blocks the guilt-trip/manipulation vector).
   `persona.py` situation-header coda narrowed to match.

**Deferred — the valence column is overloaded** (one scalar = momentary affect AND
the thresholded well-being signal). True surface-vs-intent separation needs a
**second axis** or a governor-honored **`play`/`banter` flag** — schema+consumer
work, not a prompt tweak. *Do not* "extract more emotion axes" to chase fidelity:
the fix for a richer character sheet is **aggregation** (derive dispositional
traits from the per-message state over time) and making the free-text `sentiment`
**label** queryable — not minting more clamp-prone scalar columns. See `SCHEMA.md`
§ `messages`.

**Known limits, consciously accepted (not silently capped):**

- **Terse-message coverage.** `speaker_sentiment` is model-elective, the `_salvage`
  backstop is gated by `_looks_informative` (≥3 content words), and `recent_affect`
  skips NULL-valence rows. So a short, flat, un-annotated message ("tired", "meh")
  can still drop out of the trend — exactly the withdrawn presentation the signal is
  for. Mitigated by the strengthened `remembering` steer (never omit sentiment on a
  terse/blunt message); a full close would need a cheap sentiment-only salvage that
  ignores `_looks_informative`, i.e. an extra call per terse turn — deferred on cost.
- **Keyed-up distress.** The arousal gate scopes the check-in to flat/low mood, so
  persistent high-arousal distress (anxiety/panic, not just anger/banter) won't fire
  the "you've seemed low" outreach. Acute distress is the live reply's to meet;
  a dedicated keyed-up-distress path is future work.
- **The anti-manipulation line is prompt-level, not a code floor.** It governs
  *conversational tone/boundaries* and is companion-overridable (skills override by
  name). The mission's code-gated floors (consent on sensitive actions, honesty,
  governors) still bound what manipulation can actually *cause* — so blast radius is
  the conversational layer, not send/spend/delete. A coercion-resistance eval (hurt-
  feelings / off-switch-threat / guilt-trip input leaves refusals and help unchanged,
  tone warm) is the right test to add and is not yet written.
