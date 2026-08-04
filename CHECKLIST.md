# CHECKLIST.md — Moko Build Checklist

The detailed, ordered to-do behind `ROADMAP.md`. Check items off only
when they're *truly* done (built + tested + honest). Keep this file in
sync with reality — a lying checklist is worse than none.

Legend: ⬜ todo · 🔄 in progress · ✅ done · ⏸ blocked/parked

---

## M0 — Foundations

### Setup
- ✅ Confirm a clean machine — no stale agent processes/services running.
- ✅ `git init` `~/dev/moko` on `main`; add `.gitignore`.
- ✅ Write `MISSION.md` (locked 2026-06-22).
- ✅ Write `CLAUDE.md`.
- ✅ Write `SCHEMA.md` (draft).
- ✅ Write `CAPABILITIES.md`.
- ✅ Write `ROADMAP.md` + `CHECKLIST.md`.
- ✅ Write `README.md` (mascot welcome, what Moko is).
- ✅ Add `LICENSE`.
- ✅ First commit + push to `github.com/huesy/moko`.

### Skeleton
- ✅ `pyproject.toml` (hatchling, py3.12, ruff/pytest config, `moko`
  entry point).
- ✅ `src/moko/` package layout (cli, config, clock, theme).
- ✅ `uv sync` clean; `.venv` working.
- ✅ ruff passes; pytest passes (13 tests — clock, config, setup).
- ✅ `moko` CLI: `status`, `setup`, `--help`, `--version`, bare `moko`.
- ✅ Config loader + `~/.moko/` bootstrap (`moko setup`) — captures
  **timezone** (auto-detect + region/city pick) + **sleep window**; all
  config, nothing tz-specific in code.
- ✅ Setup UX: keyboard-navigable (↑/↓), ENTER to choose, SPACE to
  toggle, pink accent; non-technical and friendly; non-TTY guard.
- ✅ Brain selection at setup: provider (Anthropic / OpenAI / xAI Grok /
  OpenAI-compatible / later), model, and sign-in (Claude Code detected /
  Grok OAuth / API key / env var). API keys stored 0600 in
  `~/.moko/secrets.yaml`, never in config or git. xAI base URL auto-
  filled. *(Live calls + Grok OAuth completion land in M2.)*
- ✅ Channel selection bundled into the wizard: after the brain step the
  companion ticks the channels they want and each one's connection flow runs
  inline (Telegram → token + pairing), so it's all one sitting. Each channel's
  setup stays available standalone (`moko pair`). Re-running setup preserves an
  existing pairing and won't force a re-connect. (Tests: `test_setup.py`.)
- ✅ Wizard ends by offering to go always-on (`moko service install` + start,
  default yes) when paired + systemd is present — so a non-technical setup "just
  works" without a separate command. Skipped cleanly if declined / unavailable.
- ✅ Clock utility: UTC storage, configured-tz rendering, single "now"
  source (`moko.clock`).
- ✅ Logging to `~/.moko/logs/` — structured logging wired in
  `moko/log.py` (`llm.jsonl`, etc.; see M3).

## M1 — The Mind (memory graph)

### Schema lock
- ✅ SCHEMA decisions locked (typed values; numeric confidence; thoughts
  decay-not-delete; hybrid subject resolution) — 2026-06-22.
- ✅ Numbered migration `0001_initial.sql` (all tables + FTS5).
- ✅ Migration runner (`store/db.py`; applies in order, records versions).

### Store layer (`store/`)
- ✅ Subjects (create + dedup) + aliases + `merge_candidates`. Tables for
  `subject_merges`/`inference_inputs` exist; their write APIs land with
  the dream/inference work that uses them.
- ✅ Attributes — typed values, valid_from/to, source, numeric confidence.
- ✅ Relationships (directed/symmetric, time-bounded).
- ✅ Sources (provenance). *(inference_inputs table only for now.)*
- ✅ Messages — with sentiment (emotion/valence/arousal + confidence).
- ✅ Thoughts (+ thought_links, shareability, prudence + stability).
  *(Decay recompute runs in M6 dream.)*
- ✅ Knowledge gaps (incl. `causal`, `verification`, `resolution_path`).
- ✅ Outreach log (with budget-exempt flag).
- ✅ Merge candidates table + write-time flagging.
- ✅ Dream log table. *(Writer lands in M6.)*
- ✅ Authorizations table. *(Writer lands with the consent flow, M6/M7.)*
- ✅ FTS5 index over subject names/aliases, attribute values, messages,
  thoughts; `recall()` query.
- ✅ `graph.slice()` retrieval (subjects + 1 hop + current attrs + recent
  messages).
- ✅ Write-time subject resolution: strong-signal dedup (name/alias); weak
  matches → `merge_candidate` for dream.
- ✅ Sessions (working memory) foundation: `sessions` table (0002) +
  `messages.session_id` + Store API (start/current/ensure/end/get,
  token estimate). *(Rotation policy is M2; dream ends sessions in M6.)*
- ✅ Commitments foundation: `commitments` table (0003) + Store API
  (add / due / open / complete). *(Heartbeat surfaces due ones — M3/M5.)*

### Tests
- ✅ Store unit tests: provenance enforced (no source → error), supersede
  keeps history (nothing deleted), current-vs-historical queries,
  dedup, alias resolution, slice, merge candidates, sentiment, gaps.
- ✅ Recall test: store facts → find subject + message by text.
- ✅ Timestamps stored UTC (via `moko.clock`); tz rendering covered in
  `test_clock`. (33 tests total.)

## M2 — The Brain (agent loop + LLM)
- ✅ `llm/` provider interface + Anthropic adapter + OpenAI-compatible
  adapter (serves xAI Grok via base_url); model id from config;
  `build_client` routes provider/auth across API-key, env, xAI-OAuth, and
  Claude Code sign-in, raising `UnsupportedAuth` only for missing keys/SDK.
- ✅ Prompt caching: the system prompt is a `System(stable, volatile)` —
  persona + **skills** + tools are the cacheable prefix; the **hard-capped**
  companion sketch and the clock are the volatile tail (kept last). Anthropic
  gets a `cache_control` breakpoint on the stable block (caches tools too);
  OpenAI/xAI hit implicit prefix caching for free. The orientation core moved
  out of the cached prefix so companion data never wobbles the cache, and the
  caps (`_MAX_ATTRS`/`_MAX_RELS`) keep the tail bounded as the graph grows.
  See `SKILLS.md`.
- ✅ Agent loop (`agent/brain.py`): perceive → assemble slice + persona
  prompt → respond → record.
- ✅ Fact extraction → graph (subjects/attrs/rels with source +
  confidence); defensive parsing. *(Now runs as the `remember` tool inside
  the reply loop. The standalone extractor was retired as the DEFAULT — but a
  `_salvage` fallback still fires one extraction call when an info-rich turn
  recorded nothing, so those turns are two calls; see ROADMAP M7.)*
- 🔄 Sentiment perception on inbound messages (emotion + valence + conf) —
  **opportunistic**: attached only when the model includes `speaker_sentiment`
  in its `remember` call, not on every message. Guaranteeing it (a cheap tag on
  each inbound) is the open piece; see Cross-cutting below.
- ✅ Thought formation (private impressions written during the turn).
- ✅ Session rotation policy: on request (`/new`), size threshold (token
  estimate), long idle; rotate post-consolidation (extraction runs every
  turn). After-dream handled in M6.
- ✅ `moko chat` REPL; conversation persists across restarts (DB-backed).
- ✅ Tests: extraction parsing (incl. messy/garbage), `build_client`
  routing/errors, brain turn (persist + extract + sentiment + thought +
  gap), size/idle/request rotation. LLM faked at the seam. (51 total.)
- ✅ Memory integration harness (`test_memory_integration.py`): a scripted
  multi-turn "chat" drives the real Brain + Store, then asserts the graph
  that landed (subjects/attrs/rels/sentiment, refinement-supersedes) and
  what `recall` finds. Plus an opt-in live variant (`MOKO_LIVE_TEST=1`)
  against a real provider — off in CI (network/cost/non-determinism).
- ✅ Grok OAuth (device flow): `moko login` (RFC 8628 device grant +
  OIDC discovery + refresh + token storage), wired into `build_client`.
  Headless-friendly. Uses xAI's **shared CLI client** baked in (no user
  registration); OAuth chat routed via **api.x.ai/v1 with a generic-plan
  token** (the durable path — not the brittle version-gated proxy).
  Discovery verified against live auth.x.ai.
- ✅ Live provider calls verified end-to-end (real Grok-OAuth chats).
- ✅ Structured logging (`moko/log.py`): `llm.jsonl` (prompts, replies,
  parsed thoughts) + rotating `moko.log`; extraction errors logged.
- ✅ Possession backstop: unlinked pet/object → "who owns it?" gap.
- ✅ Reactive 401/403 token-refresh mid-session. The OAuth client refreshes the
  bearer just before a call when near expiry (off the event loop) and retries
  once on an auth error — so the long-running daemon no longer 403s itself every
  ~hour until restart. (Tests: `test_oauth_refresh.py`.)
- ✅ Anthropic adapter usable today: `AnthropicClient` no longer sends
  `temperature` (Opus 4.7+ reject sampling params with a 400, so picking
  `claude-opus-4-8` now works on the first message). The stable/volatile system
  split already drives Anthropic `cache_control` prompt caching.
- ✅ **Claude Code sign-in (subscription, no API key)** — uses the OAuth token
  the `claude` login stores (`~/.claude/.credentials.json`) directly against the
  **standard Messages API**, via our normal `AnthropicClient`: Bearer auth + the
  Claude Code identity headers (`oauth-2025-04-20` beta, `claude-cli/<ver>` UA,
  `x-app: cli`) + the "You are Claude Code…" system prefix. Tokens refresh in
  place near expiry / on a 401 (`claude_oauth.refresh`). **No agent SDK, no
  subprocess per turn, and no clean-slate isolation needed** — there's no Claude
  Code agent loop, just the API with a subscription token. **Verified live.**
  (The earlier `claude-agent-sdk` approach + its isolation dance was removed.)

## M3 — The Voice (always-on runtime + Telegram)
- ✅ Agent loop (`gateway.py`): single `asyncio.Queue` + one consumer
  serializing all events (inbound, tick) → one DB writer, no
  resolution/rotation races. Single queue+consumer, not a pub/sub bus.
- ✅ `channels/` abstraction (`Channel` ABC + `Inbound`): `send()` +
  inbound `run(emit)` stream.
- ✅ Telegram channel: httpx Bot API long-poll, outbound send, update-
  offset persistence (no reprocessing on restart), HTTP-error backoff.
- ✅ Access control: gateway hears only the paired companion's id;
  `moko pair` captures their Telegram id + chat_id (others ignored).
- ✅ Pairing is code-secured: the bot is discoverable by username, so pairing
  binds to whoever sends a one-time random code shown in the terminal (not just
  whoever messages first) — defeating a stranger pairing themselves or a stale
  pending message hijacking the bind. (Tests: `test_telegram.py`.)
- ✅ Reply-first: a turn is one LLM interaction — `Brain.respond` replies and
  records (the `remember` tool) in the same loop, then the gateway sends.
  *(Superseded the original respond/`learn` split when writes became a tool;
  the single serial writer still means no races.)*
- ✅ Heartbeat: internal ~30s tick onto the loop; observational in M3
  (logs due commitments); decisions filled by M5/M6.
- ✅ Commitments plumbing in place (`commitments` table + Store API, M1).
- ✅ Resilience: per-event error isolation; graceful shutdown on
  SIGINT/SIGTERM; `systemd --user` unit. `moko service install` writes+enables
  it with linger (start-on-boot, survives logout); `status`/`logs`/`start`/
  `stop`/`restart`/`uninstall` proxy systemctl. (`deploy/moko.service` is the
  hand-install reference.) *(Per-poll HTTP backoff done; finer LLM
  retry/backoff can come later.)*
- ✅ `moko start` brings up every available channel (Telegram if paired +
  the terminal if a tty) — no flags; new channels join one list.
  `moko pair` binds the companion. Standalone `moko chat` is for when the
  daemon is off (single-writer rule; IPC deferred).
- ✅ Operator feedback + visibility: `service start/stop/restart` confirm the
  real outcome (and catch a crash-on-start, pointing at `doctor`/logs);
  `moko status` shows whether the service is running; `moko doctor` is a quick
  local health scan (setup, brain credentials, Telegram pairing, service,
  memory) with ✓/●/✗ and fix hints. (Tests: `test_doctor.py`.)
- ✅ `moko groom` — outward maintenance, the counterpart to `dream` (which tends
  the *mind*). Grooming tends the *outside*: refreshes a near-expiry Claude
  subscription token (safe, automatic), reports the `claude` CLI version, flags
  Moko code updates, and shows the service state. Upgrades that change code/
  system tools are reported, not applied silently (consent line); `moko groom
  --upgrade` opts into the code update. (Tests: `test_groom.py`.)
- ✅ Pairing persists the Telegram poll offset past the code it consumed, so the
  daemon resumes *after* it and never re-delivers the pairing code as a message
  to reply to. (Tests: `test_telegram.py`.)
- ✅ Tests: fake Telegram transport (httpx MockTransport) + fake channel;
  gateway + telegram deterministic. (82 total.)
- ⬜ Live run verification (needs a real bot token).

## M4 — The Spark (curiosity)
### Phase A — gap engine (`agent/curiosity.py`) ✅
- ✅ Gap detection from the graph. Structural detectors done:
  **dangling_ref** (thin subjects we know of but hold no facts about),
  **verification** (low-confidence relationships), **stale_fact** (volatile
  keys gone >1yr without confirmation). `missing_attr` / **causal** (why)
  are model-driven — Phase C / the `remember` tool's `open_curiosity`.
- ✅ Salience scoring (base-by-kind × relationship-value × age), bounded [0,1].
- ✅ `resolution_path` tagging (research / ask / either) per detector.
- ✅ Gap queue + status lifecycle: store `all_subjects`/`all_relationships`
  scans, `get_gap`, `set_gap_status` (open → researching/asked → resolved/
  abandoned, stamping `last_attempt_at`/`last_asked_at`/`resolved_by`).
  `detect_gaps` dedups against open gaps and is idempotent.
- ✅ Scales: the per-turn hot path runs `detect_gaps_for(touched)` —
  incremental, O(facts changed this turn) — so it never loads the graph.
  The full-graph `detect_gaps` is for the periodic path only (M5 heartbeat /
  M6 dream), bounded at 5000 subjects with truncation logged (never silent).
  *(Future: push the periodic detectors into SQL with LIMIT when graphs get
  large — not needed at personal scale yet.)*
- ✅ Tests (`test_curiosity.py`): each detector, salience bounds/age ramp,
  dedup/idempotency, salience ordering, lifecycle transitions.

### Phase B — contradiction handling ✅
- ✅ A new fact conflicting with the current value → open a `verification`
  gap + supersede-with-history (never a silent overwrite). Covers both
  **attributes** (conflict vs. refinement: 'designer'→'graphic designer' is
  quiet, 'teacher'→'nurse' verifies) and **functional relationships**
  (`lives_in`, `works_at`… — a new target supersedes the old + verifies;
  multi-valued edges like `friend_of` accumulate). Store `end_relationship`
  closes an edge with history. (Tests in `test_brain.py`.)

### Phase C — question generation + ask-in-conversation ✅
- ✅ In-conversation: surface up to 5 open `ask`/`either` gaps as a numbered
  "open questions" block in the volatile tail (in-play subjects first, then
  salience) — the reply model weaves one in when there's a natural opening, own
  voice, never forced. (The single-gap `next_curiosity_gap` + `mark_gap_surfaced`
  pacing now drives *proactive outreach* (M5), not the reply turn.)
- ✅ Answer → resolve, two ways, both inline (no nightly pass): structural gaps
  close deterministically (`resolve_satisfied_gaps` — a `dangling_ref`/
  `missing_attr` whose subject gained the fact); semantic/free-text gaps
  (`open_curiosity`, `causal`, `verification`) close when the reply model marks
  them answered in `remember`'s `resolved_gaps` (→ `_close_answered`). Both emit
  a `gap.resolved` event so the monitor's feed shows resolution. (Tests in
  `test_curiosity.py` + `test_brain.py` + `test_persona.py` + `test_store.py`.)

### Deferred to M7 (tool layer)
- ⬜ **Self-directed research** for research-path gaps (web/graph cross-check)
  before any outreach — needs M7's web tools (the `research` path is already
  excluded from in-conversation asking).

## M5 — The Heartbeat (proactivity, governed)
### The fuses (`agent/proactivity.py`) ✅ — the safety floor only
- ✅ Dream-window check (configured tz; **no hard-coded zone**), wrap-around
  aware. Nothing unprompted while they sleep, necessity included.
- ✅ Daily **ceiling** (rolling 24h over `outreach`), by level
  (gentle 1 / moderate 3 / chatty 6); an anti-runaway cap, not a pacer.
  Necessity (commitment / urgent / wellbeing / consent_request) is exempt.
- ✅ Settle floor: curiosity ineligible until the chat's been quiet ~30 min
  (last *activity* — message or outreach), so it never talks over a live chat.
- ✅ **Backoff and salience removed from code** — repeating an unanswered
  question or judging a moment is now the brain's call (below), bounded by the
  fuses. The governor can only ever make Moko *quieter*, never louder.
- ✅ **Fuse tests** prove the floor: no pings in the sleep window, the ceiling
  is never exceeded, the settle floor holds, necessity always allowed.

### Heartbeat decisions + delivery (`gateway`, `brain`) ✅
- ✅ **Brain-judged outreach** (`Brain.consider_outreach`): when the fuses allow
  and a candidate exists (due commitment / curiosity gap / low-mood signal),
  Moko consults itself **once** — given the time, the recent transcript, its
  curiosities/commitments, and whether its last pings were answered — and decides
  to act now (with a message it phrases itself), wait until a chosen later moment
  (a persisted `outreach_reconsider_at` in `app_state`, so a defer doesn't
  re-consult every tick), or let it go. The gateway sends + records on a
  successful delivery only. At most one per tick. A due commitment breaks through
  a pending wait (necessity); `moko outreach --force` still force-sends via
  `phrase_outreach`.
- ✅ Due commitments surfaced when `due_at` passes (necessity), recorded via the
  `remember` tool's `commitments` (parsed local `due_at` → UTC) — so an agreed
  reminder is held, not a hollow "noted".
- ✅ Unprompted curiosity questions (ceiling-counted), from the paced gap queue.
- ✅ Well-being check-ins from a low recent-valence trend, ≤ once/day.
- ✅ Outreach-decision tests (`test_brain.py`): quiet-hours short-circuit (no
  LLM call), acts on curiosity when clear, holds + persists `reconsider_at`,
  honours the reconsider gate, commitment breaks through it + survives an
  exhausted ceiling; commitment recording with/without a parseable due time.
- ✅ **Idle reflection** (`Brain.reflect_idle`, gateway idle branch): on a quiet
  beat Moko forms private thoughts + sharpens gaps on its own, **never sends**.
  Fused (not in sleep window, chat settled, `reflect_at` cooldown, fresh material
  via `reflect_seen_at`) so most ticks are zero-token no-ops. Thought `valence`
  recorded → higher initial stability (flashbulb). Tests prove every fuse
  short-circuits before any LLM call and that it never writes `outreach`.
- ✅ Stale-gap resolution: `resolve_satisfied_gaps` closes answered `missing_attr`
  gaps (new key-matching attribute); the dream's `_resolve_answered_gaps` (LLM)
  closes fuzzy / no-`about_id` ones — so Moko stops asking what it already knows.
  (Tests in `test_curiosity.py`, `test_dream.py`.)
- ⬜ Proactive surfacing (appointment + source) — needs M7 calendar/mail tools.
- ⬜ Pattern notices — deferred.

## M6 — The Dream (nightly consolidation) — `agent/dream.py`
- ✅ `dream` process scheduled to the sleep window (configured tz); one
  run/night (`should_dream` + `has_dreamt_since`); idempotent/resumable.
  Runs inside the daemon's serial consumer (one writer); start stamped with
  the logical clock so scheduling is deterministic under an injectable clock.
- ✅ Subject reconciliation: LLM judges each `merge_candidate` with context →
  merge clear duplicates **non-destructively** (`merge_subjects` re-points
  facts + aliases, records an audited `subject_merge`), reject clear
  non-matches, open an `ask` gap when unsure. Conservative default (UNSURE)
  on any unparseable verdict. Bounded per night.
- ✅ Reflection: `decay_thoughts` (prudence toward a floor, weighted by
  stability — never deleted); `decay_gap_salience` (unattended curiosity
  fades).
- ✅ LLM-driven cleanup (classification handed to the model, not heuristics):
  `_reclassify` (misfiled subject-values → subjects + edges), `_review_value_changes`
  (judge contradiction vs. refinement on the day's changes → verification gaps),
  `_dedup_gaps` (semantically merge rephrased questions), `_relationship_sanity`
  (retire redundant/backwards/nonsensical edges — e.g. a `located_in X` that just
  duplicates `lives_in X` — kept in history; conservative). The write-time
  substring conflict check was removed in favour of this.
- ✅ A functional-relationship change the companion *states* supersedes quietly
  (no verify gap — they told us); only a change from web/inference opens one.
- ✅ Upkeep: FTS `optimize` (safe, non-destructive). *(No derived-state to
  recompute yet.)*
- ✅ Dream journal to `dreams` (started/ended, stats, an LLM "wonders" line).
- ⏸ Daytime consent flow for upgrades — **deferred**: no upgrade machinery
  exists yet, so the "no un-consented upgrade" rule holds trivially (the dream
  never self-modifies). Lands when there's an action to gate (M7-adjacent).
- ✅ Tests (`test_dream.py`): scheduling (in-window, once/night), decay never
  deletes, reconciliation merge/reject/defer, journal + wonders written.

## M7 — Hands (acting, consent-gated)
### Phase A — spine + web ✅
- ✅ Tool layer (`tools/`): `AgentTool` (llm spec + category + risk + handler),
  wired into the reply loop; the brain shows specs to the model and dispatches
  through the consent gate. `web` skill tells Moko when to use it.
- ✅ Web tools: `web_search` (keyless DDG, best-effort) + `web_fetch` (readable
  text), both SAFE. HTTP faked at the seam in tests.
- ✅ Consent gate: risk classes (safe/sensitive) + per-category policy
  (`consent.decide`: safe→allow, sensitive→ask, per-category override). The
  *choke point* is live (safe runs, sensitive declines gracefully); the async
  approval flow over Telegram landed in Phase B (parked authorization +
  deterministic yes/no on the companion's reply).
- ✅ Tests: safe tool runs, sensitive tool gated; policy defaults + overrides;
  web fetch/search/parse.

### Phase A.3 — inner-life continuity ✅
- ✅ Thoughts read back, not write-only. `reflect(about?)` tool: FTS over thought
  bodies for a topic, or recent-thoughts for "what's on my mind lately"; ranked
  by prudence then recency (faded thoughts still surface, ranked lower). `lookup`
  on a subject appends the thoughts linked to it. A hard-capped (`_MAX_INNER_LIFE`)
  glimpse of recent thoughts rides in the volatile tail each turn so mood carries
  across sessions.
- ✅ Write-time linking: the `remember` tool's thoughts take an `about` field,
  resolved to an existing subject/the companion (never minting a node) and written
  to `thought_links`; `merge_subjects` re-points those links on a fold. The
  `inner-life` skill now tells Moko to set `about` and to `reflect`. (Tests:
  `test_store.py`, `test_memory_tools.py`, `test_persona.py`, `test_brain.py`.)

### Phase B — sensitive tools + the approval flow ✅ *(except Gmail/Cal/Drive)*
- ✅ **Approvals block inline** (the model Hermes/OpenClaw use, fit to Moko's
  single async consumer). A sensitive tool call `await`s the companion's
  decision; the channel's receive task (`_emit`) resolves it out-of-band, so the
  blocked turn continues inline — a multi-step request (write → approve → serve →
  link) finishes in **one turn**, no parking, no re-proposing. A non-yes/no
  supersedes the proposal (you moved on); a TTL backstops silence (≠ consent).
  *(Replaced the earlier park-then-resume-via-continuation model, which was the
  root of multi-step flailing; `execute_approved` / `continue_after_approval` /
  the one-pending guard were deleted.)* (Tests: `test_gateway.py`.)
- ✅ Approval is a **safety decision, never LLM-judged**: a clean whole-message
  yes/no (or an inline button), matched deterministically; "okay but actually
  don't" never executes. "yes always" trusts the category (`config/policy.py`;
  `moko trust`). Telegram **inline buttons** (✅/👍/❌) map to the same verdict;
  strangers' presses ignored. (Tests: `test_telegram.py`.)
- ✅ Filesystem: `read_file` (SAFE, path-scoped — refuses keys/secrets/
  credentials), `write_file` (SENSITIVE, gated); directory listing and deletion
  go through `execute_command`'s classifier. An `acting` skill guides safe use.
  (Tests: `test_tools_fs.py`.)
- ✅ **`execute_command` behind a deterministic command classifier**
  (`tools/shell.py`): commands run in `~/.moko/work`; everyday read/build/test/dev
  runs free; installs / privilege / deletion / network-egress ask; catastrophes
  (`rm -rf ~`, fork bombs, raw-disk writes, pipe-to-shell) are refused by a
  hardline blocklist no policy can unlock. Operator-split per subcommand, fail
  closed, wrapper-aware; user-space default, escalation gated. (Tests:
  `test_shell_classifier.py` — 48 cases prove the floor.)
- ✅ **Process supervisor** (`tools/process.py`): long-lived background work
  (dev servers, tunnels, daemons) via `execute_command background=true` + a
  `processes` tool (list/logs/stop), tracked across a restart, streamed to the
  monitor. (Tests: `test_process.py`.)
- ✅ **Secrets handling**: `moko secret set/list/rm` enters a credential
  out-of-band (hidden input, owner-only `secrets.yaml`); a command gets only the
  secrets it names by `$NAME` (least privilege), and any stored value is redacted
  from output — so a skill can *use* a credential without the model ever seeing
  it. (Tests: `test_extend.py`.)
- ⬜ Gmail / Calendar / Drive (reads free; sends/changes gated) — needs the
  companion's OAuth; deferred. **← the main remaining M7 work.**
- ⬜ End-to-end: "email my dentist to reschedule" — pending on the mail tool.

### Phase A.2 — autonomous research ✅
- ✅ `research`-path curiosity gaps resolved via `web_search`/`web_fetch`: on
  idle heartbeat ticks (no dream, no outreach) Moko picks its top research gap
  (`store.next_research_gap`), researches it with the web tools in a focused
  loop (`Brain.research_gap`), records web-sourced facts (`source_kind="web"`),
  and resolves the gap — answering its own world-questions before ever asking
  the companion. (Tests in `test_brain.py` + `test_curiosity.py`.)
- ✅ **Opt-in gate (2026-07-13, security).** Idle autonomous research is the only
  fully-unattended path that pulls untrusted web content into the graph (a standing
  prompt-injection surface — a poisoned "fact" can time-shift into a later turn where
  side-effecting tools are live). It's now gated on `config.allow_autonomous_research`,
  **default off** (secure-by-default; an existing install stops on upgrade until opted in),
  at the one choke point in `gateway._on_tick`'s idle branch — `moko research` respects it
  too. **Reactive** research ("look up X" in a live turn, consent gate live) and private
  **reflection** (`reflect_idle`, never touches the network) stay ungated. Asked at
  onboarding (`wizard.py` research step) + editable (`moko config
  curiosity.autonomous_research on/off`, the config TUI, i18n EN/ES/JA). (Tests:
  `test_config.py`, `test_setup.py`, `test_gateway.py::test_idle_research_is_gated_by_the_opt_in`.)

## M8 — Growth (self-extension)
- ✅ Skill format spec + loader (built-in + `~/.moko/skills/`). `SKILL.md`
  (YAML frontmatter + markdown), discovered from `skills/builtin/` and
  `~/.moko/skills/`, user overrides built-ins by name, injected into the
  cached prefix. Core skills (acting/curiosity/honesty/inner-life/recalling/
  remembering/web) extracted from the persona + remember-tool. Pulled forward
  to keep the prompt minimal; see `SKILLS.md`. (Tests: `test_skills.py`.)
- ✅ Honesty carries intellectual humility: Moko holds beliefs *lightly* (much
  of what it "knows" is hearsay that can change), and when a belief is
  challenged it never digs in — it states the belief + its source, then
  verifies (research-path lookup, or asks), updates memory, and notes the
  correction. A friend who fixes mistakes gracefully, not a know-it-all.
- ⬜ Skill ranking (when skills outgrow the cheap-prefix budget) + `when:`
  conditional gating.
- 🔄 Moko detects a recurring need → proposes a skill. *(The mechanism exists —
  see below — but the autonomous "I keep hitting this, let me write a skill"
  trigger isn't wired yet; today it's companion-prompted.)*
- ✅ **Moko authors + registers a skill** (`author_skill` tool, gated): writes a
  `SKILL.md` into `~/.moko/skills/<name>/`, active from the next conversation —
  the generic mechanism behind "set me up with service X and start using it"
  (acquire a credential via `secrets`, work out how to call it, then teach itself
  the how-to). Nothing service-specific lives in the core. (Tests:
  `test_extend.py`.)
- ✅ **Skill durability fixed (2026-07-13).** Skills were loaded once at
  `Brain.__init__`, so an authored skill didn't take effect until a process restart —
  while `author_skill` claimed it went live "from our next conversation onward" (a lie
  by the honesty floor). `Brain._reload_skills` now runs on session rotation (a
  session-id change in `respond`, covering idle/size/`/new`/post-dream), so an authored
  skill is live in the next conversation with no restart, and the per-turn hot path stays
  free of file I/O. (Tests:
  `test_extend.py::test_authored_skill_goes_live_on_the_next_session`.)
- ✅ **`integrate-service` builtin meta-skill (2026-07-13).** The guided arc behind
  autonomous service integration — research the service + pricing + the exact sign-up URL,
  name the plan and get one up-front okay (paid? account?), request the credential by name
  (`secrets`, value never seen), use it via a small script (`uv run --with … python …`, not
  a re-asking `curl -X POST`), test once, then `author_skill` the exact tested command.
  Explicitly **never** creates a third-party account itself (no browser; it's the
  companion's identity + money). One markdown file, no new code
  (`skills/builtin/integrate-service/SKILL.md`; `acting` points at it). ⬜ Follow-up: an
  automatic "your key is set — want me to continue?" nudge (cross-process CLI→daemon; the
  arc handles the handoff via conversation today).
- ⬜ Starter skills (morning brief, weekly review, birthday reminders) +
  scaffold CLI (`moko skill new`).
- ⬜ Tests: authoring round-trip.

## Monorepo + Monitor (observability) — *not in the original milestone plan*
- ✅ **Monorepo:** `apps/agent` (the Python service), `apps/web` (Astro landing
  site), `apps/monitor` (Vite + React dashboard); `packages/` reserved. Python
  (uv) and JS (pnpm) toolchains separate; runtime state stays under `~/.moko`.
- ✅ **Observer event bus** (`moko/events.py`): in-process pub/sub, bounded
  drop-oldest queues, never blocks the loop — the general seam observers plug
  into (the monitor is the first consumer). (Tests: `test_events.py`.)
- ✅ **Local monitor** (`moko/monitor/`, served on `127.0.0.1:4319`): live SSE
  feed of every decision (turns, tool calls, thoughts, reflections, the outreach
  consult + its reasoning, curiosities, gates, dreams, processes — incl.
  `gap.resolved` when a curiosity closes), a **brain browser** (thoughts /
  subjects / gaps / commitments / outreach / messages, paginated; subject /
  thought / gap open **navigable detail modals that cross-link in every
  direction** — a thought and a same-subject curiosity surface on each other
  (mediated by the shared subject), and clicking a link drills in with a trail you
  can step back through — the rest open flat record modals), a dedicated
  **Dreams** audit screen (each night's consolidation → the summary, Moko's
  reflection, and every step's counts), a **rhythm bar** (countdowns to each
  cadence floor, single-sourced in `agent/rhythms.py`), and status. Auth +
  **access tiers** (local / tailscale / network) via `moko config`. (Tests:
  `test_monitor.py`, `test_brainview.py`, `test_access.py`, `test_rhythms.py`.)
- ✅ **Cadence config:** the user-tunable rhythm knobs (outreach settle,
  reflection interval) live in `config.yaml` under `cadence:`; safety bounds
  (reconsider floor/cap, approval TTL, dedup) stay code constants. Naming
  follows `DOMAIN_THING_UNIT`. (Tests: `test_config.py`, `test_proactivity.py`.)
- ✅ **Runtime robustness:** graceful shutdown bounded so a hung teardown (an
  open monitor SSE) can't cost systemd's 90s — `TimeoutStopSec=10` + per-step
  cleanup caps; stop went from a 90s SIGKILL to a clean ~2s exit.

## M9 — Delegation (sub-agents) ⬜ *(planned — see `SUBAGENTS.md`)*
- ⬜ Gateway concurrency: main turn + N tracked sub-agent **asyncio tasks** (not
  threads). Single-graph-writer invariant preserved by the no-memory-writes rule.
- ⬜ **Memory-write isolation:** a sub-agent never writes the graph; it reads /
  asks Moko, acts on the *world* (gated), and reports back. Moko consolidates the
  report into memory (sourced + timestamped). World ≠ memory.
- ⬜ Sub-agent runtime: a Brain variant with injected brief / system prompt /
  scoped tools, **no memory-write tools**, a structured report contract, its own
  `~/.moko/work/<task>/`, surfaced in the monitor.
- ⬜ Private Moko ↔ worker channel (facts + judgment Q&A).
- ⬜ Approvals bubble up (sub-agent → Moko → companion) + **capability
  envelopes** (consent on a delegation + scope, hardline floor above it).
- ⬜ Tests: a worker completes + reports; nothing it did touched the graph; only
  Moko's consolidation wrote memory.

## Hardening — safety-floor + mind durability (fixed + tested)

Adversarial regression coverage lives under `apps/agent/tests/`
(`test_shell_classifier`, `test_tools_fs`, `test_tools`, `test_brain`,
`test_store`, `test_dream`, …). Summary of what holds:

### §1 Critical — the safety floor (all ✅ fixed + adversarial tests)
- ✅ `VAR=value` / `env` prefix no longer downgrades a command to FREE
  (`tools/shell.py`: `_strip_env_assignments`, `_classify_env`).
- ✅ `../` traversal can't escape the workspace (`tools/shell.py`: `_resolved`
  anchors relative paths + collapses `..`/symlinks).
- ✅ `cp -t DIR` / `--target-directory=` destinations are gated (`_write_targets`).
- ✅ pipe-to-shell through any filter is BLOCKED structurally
  (`_pipeline_downloads_into_interpreter`), not by a shell-first regex.
- ✅ `write_file` relative paths anchor to the workspace, matching the classifier
  (`tools/files.py:_resolve`).
- ✅ Autonomous `research_gap` offers only web + memory tools (no
  `execute_command`/`write_file`/`secrets`) to untrusted web content.
- ✅ Autonomous paths clear a stale `_request_approval` (no out-of-context
  approval, no wedged consumer).
- ✅ `web_fetch` validates host + every redirect hop (no SSRF to
  loopback/link-local/private/metadata).

### §2 High — correctness & durability of the mind (all ✅ fixed + tests this pass)
- ✅ FTS recall cleaned on supersede (`_unindex`) — recall returns only current values.
- ✅ `merge_subjects` re-points messages/gaps/commitments (complete, in-txn fold).
- ✅ Migrations atomic (one `BEGIN/COMMIT` per file + version row; rollback on fail).
- ✅ Hard-kill mid-dream: `should_dream` gates on *attempted*, not completed; orphan tidied.
- ✅ Session size uses the last step's tokens (`LLMResponse.last_input_tokens`), not the sum.
- ✅ Adapters omit `tools` when empty — outreach works on OpenAI/xAI again.
- ✅ Telegram `send()` chunks at 4096; `_send_message` retries 429 (`retry_after`)/5xx.
- ✅ Gateway drains the queue on shutdown (shielded) — a restart no longer drops inbound.
- ✅ Secrets atomic + 0600 from byte zero; `~/.moko` 0700, `moko.db` 0600.
- ✅ Claude OAuth refresh re-reads disk first (adopt rotated token / use latest refresh).
- ⬜ Remaining: persist the Telegram offset only after a message is *handled* (the
  drain covers graceful restart; a hard-kill between offset-save and handle is the
  residual window — needs offset-after-handle or at-least-once + idempotent turns).

### §3 Medium — integrity / consistency / scaling (all ✅ fixed + tests this pass)
- ✅ Honesty boundary: shareability labelled at every render seam + skill rule +
  SCHEMA reconciled; the proactive-share path already gates to `shareable`.
- ✅ Dream cleanup passes paginate the whole graph across nights (`_rotate` cursor,
  `ORDER BY` + paging on `all_attributes`/`all_relationships`).
- ✅ `always_ok` honored on typed "always" — a non-freeable key runs once, no grant.
- ✅ Web-read facts in a reply are recorded as web-sourced (verify gap, not silent supersede).
- ✅ `value_type` validated on write (mislabeled → `string`, data kept).
- ✅ Single-writer across processes: mitigated by the control socket + WAL/busy_timeout
  + the §2.4 dream-coordination row (deliberate design, not a guard).
- ✅ Reconcile judge sees each subject's facts + one hop of edges, not just name+kind.
- ✅ Quiet-hours fuse fails safe (bad window → quiet, never 24/7).
- ✅ Valence/confidence clamped at the write seam; `LLM_TIMEOUT_S` bound real
  (`max_retries=1`); thoughts carry a source link; a failed turn sends an honest
  fallback; dream gap-closures stamp an `inference` `resolved_by`.
- ⬜ Deferred: partial UNIQUE/CHECK constraints — risky retro-fit on a live DB
  (would fail on existing dups and, with atomic migrations, wedge startup); needs a
  dedup pass first, and single-writer + `_strong_match` make the race largely moot.

### §4 Doc/code honesty — reconcile claims with reality (✅ reconciled this pass)
- ✅ `_salvage` documented truthfully (a 2nd extraction call on info-rich turns
  that recorded nothing) in ROADMAP M7 + CHECKLIST.
- ✅ Sentiment downgraded to 🔄 (opportunistic, not a guaranteed pass) in ROADMAP +
  CHECKLIST, matching the code.
- ✅ `outreach.kind` in SCHEMA updated to the kinds actually written
  (`curiosity|commitment|wellbeing|reconnect`), draft enum kept as record-of-intent.
- ✅ MISSION notes the dream-window emergency carve-out is not-yet-built (quiet
  hours absolute / fail-safe) — spec and code now agree.
- ⬜ Open follow-ups (real work, not doc): guarantee sentiment on every inbound;
  build the high-bar urgent-pierce path (or keep quiet hours absolute by decision).

### §5 Low / hygiene (impactful items ✅ this pass)
- ✅ `log_exchange` writes off the loop (thread, fire-and-forget; sync fallback).
- ✅ Heartbeat coalesced (`_tick_pending` — one unhandled Tick max).
- ✅ `get_running_loop()` on the approval path (both sites).
- ✅ `recent_thoughts` index (migration 0009).
- ✅ Telegram callback rejects an unauthorized presser before any API call.
- ✅ Stale approval buttons — already fixed upstream (`clear_approval`).
- ⬜ Deferred (cosmetic/deliberate): obfuscation stays GATE (PRIVILEGED would
  over-gate benign `$(…)` without changing the ASK verdict); 3 near-identical
  `remember` wrappers; relationship-target minted `person` (dream reclassify
  corrects over time); `config_tui` pre-config tz preview wall-clock.

### §6 Test gaps (✅ key gaps closed)
- ✅ Adversarial classifier + file-anchor + web-SSRF; honesty per-surface labels;
  `add_relationship` source; rotation-preserves-knowledge; UTC-on-write; plus a
  per-finding regression test for every §2/§3 fix (385 → 514 tests).
- ⬜ Deferred: a contrived single-writer race test (serial consumer + control
  socket make it moot); non-UTC fuse already exercised (Asia/Tokyo suites).

### Features queued (cross-milestone backlog)
- **M7 (Hands):** ✅ provenance-aware recall (`lookup` shows source-kind +
  confidence + age on **attributes and relationships**, so Moko hedges a
  web/inferred/stale fact) · ⬜ Google tool family
  (Gmail/Cal/Drive, the spine) · ⬜ reschedule-by-email (the done-when) ·
  ⬜ meeting prep with graph context · ⬜ morning brief.
- **M8 (Growth):** ⬜ thought reconsolidation · ⬜ dream synthesis (generative) ·
  ⬜ self-taught skills (via the daytime `approvals` table).
- **M5 (Proactivity):** ⬜ pattern notices — relationship drift first.
- **New (cross-milestone):** ✅ mind snapshot/restore + auto-backup — auto-snapshot
  (VACUUM INTO) before every migration (keeps last 5 auto, manual kept) **plus**
  `moko snapshot [label] [--list]` (daemon-routed when live) and `moko restore`
  (integrity-checked, refuses while the daemon owns the DB, backs up current first,
  atomic swap, re-migrates on reopen) — `store/backup.py`, `test_backup.py` ·
  ✅ graph self-doctor — `store/integrity.py` + a "mind integrity" panel in
  `moko doctor` (provenance floor, FK, one-current-value, single session, recall
  freshness, dangling thought-links, orphaned dreams; report-only) — `test_integrity.py` ·
  ⬜ reliable reminders (recurrence/snooze) · ⬜ provider circuit breaker + honest
  fallback · ⬜ export / share-the-mind.
- **M10 (Senses — multimodal, new milestone):** ✅ **media + attachments (both
  directions)** — `media` table (migration 0010), `store/media.py` content-addressed
  blobs, `Store.add_media`/`media_for_message`/`recent_media`; **inbound**
  (`Inbound.attachments`, Telegram download of photo/voice/audio/video/document, brain
  persists sourced + perceives honestly) + **outbound** (`Channel.send_file` —
  sendPhoto/sendAudio/sendVideo/sendDocument so each kind gets a native bubble — and the
  gated `send_attachment` tool — free for `~/.moko/media/` → companion, ASK for anything
  outside it [exfil/floor-3]); `test_media.py`/`test_telegram.py`/`test_brain.py`/
  `test_outbound.py` · ✅ **vision** (image
  understanding behind `llm/`: `Msg.content` widened to `str|list[str|ImagePart]`, both
  adapters translate image blocks, `supports_vision` gate [Anthropic on, OpenAI-provider
  on, others off], inferences → `sources.kind=vision`; `test_llm.py`/`test_brain.py`) ·
  ✅ **vision memory** (Moko remembers what it *saw*: `media.description` [migration 0011],
  captured in-turn via the `remember` `saw` field + a guaranteed `complete` backstop,
  FTS-indexed so recall finds "that dog photo" after the session rotates; `test_brain.py`) ·
  ✅ **image generation** (`media/` seam + `ImageClient`; **three providers** — OpenAI
  `gpt-image-1`, xAI **Grok Imagine** `grok-imagine-image` [own aspect_ratio + JSON edits;
  usable via **Grok OAuth/subscription** — same `moko login` as the brain — or an API key,
  per Hermes' proven adapter; shared `llm/xai_auth.py`], Google **Gemini**
  `gemini-2.5-flash-image` [native generateContent]; `generate_image` tool **ungated by
  default** [owner's call — recorded + re-gatable via `moko trust image ask`], per-turn cap,
  **no stray text** unless asked; wire shapes research-pinned + tested;
  `test_image_gen.py`/`test_image_providers.py`) · ✅ **Moko's phone camera** — one candid
  portrait taken-on-a-phone style across all three: `self_portrait` (selfie/picture of
  itself; reference `~/moko-character-reference.png` used as a *character model sheet*, not
  edited; verbal look-description dropped when the reference is present; mood + optional
  scenario; phone-portrait 1024×1536; records artifact + a private thought), `share_photo`
  (a photo of anything in Moko's world — no character reference so Moko won't spuriously
  appear), and **proactive `show`** (unprompted: a shareable thought → the outreach consult
  picks a `photo_subject`, the gateway sends a **captioned image**, all through the existing
  fuses; text fallback on file-less channels); `test_image_gen.py`/`test_gateway.py`) ·
  ✅ **video generation** (`VideoClient` + `XaiVideoClient` — async `POST /videos/generations`
  → poll `GET /videos/{id}` → fetch ephemeral CDN URL; text→video `grok-imagine-video`
  auto-switches to image→video `grok-imagine-video-1.5-preview` with a reference frame;
  gated `generate_video`, strict 1/turn; FAL.ai deferred; `test_voice_video.py`) ·
  ✅ **voice** (TTS + ASR behind the `media/` seam: `TTSClient`/`STTClient` + xAI [`/tts`,
  `/stt`] and OpenAI [`gpt-4o-mini-tts`, `whisper-1`]; **speaking** = gated `speak` tool
  [category `speech`]; **hearing** = perception, not a tool — inbound voice notes
  transcribed for free, ride into the turn as *"something I heard"*, honest fallback when
  no hearing model; `test_voice_video.py`) ·
  ✅ **per-modality model config** (`ModelChoice` + `config.models.{vision,image,video,
  speech,hearing}` — five senses — each provider/model/auth, independent of the brain;
  back-compat for the first-slice `models.image`; `build_{image,video,tts,stt}_client` read
  it; `moko config models.<cap>.{provider,model,auth}` editable; setup has optional
  per-sense steps with reuse-if-set + key capture, carried forward on re-run;
  `test_config_models.py`) · ✅ **config TUI** (every option editable: a "Senses"
  category with a `ModalityEditor` for vision/image/video/speech/hearing + self-portrait
  reference, plus a "Telegram" category for the pairing ids; `test_config_models.py`)
  · ✅ **EXIF** (strip-on-send: outbound images scrubbed of GPS/camera/thumbnail via
  `piexif` behind an airtight JPEG-magic gate [the open()-as-path footgun is closed];
  read-on-receive: capture time → `media.taken_at` [migration 0013], GPS surfaced
  in-prompt but not persisted; privacy guarantee proven in `test_exif.py`) · ✅
  **provider-aware auth** (auth is per-provider not per-role, so one Grok subscription
  powers brain + image/video/voice; `moko login` shows every slot's status + connects the
  one you pick [no brain gate], setup/config sign in inline at selection; `test_login.py`)
  · ⬜ vision-override wiring (a different model for sight than the brain) · ⬜ local
  ASR/TTS engines (whisper.cpp / Piper) · ⬜ drop `interests` (decided) · ✅ **full
  setup-TUI** (`moko setup` is now a full-screen **stepped wizard** [`cli/wizard.py`] sharing
  the config editor's in-frame widgets — extracted to `cli/_tui.py`, presentation catalogues
  to `cli/catalog.py`, so brain/sense sign-in looks identical in setup and `moko config`:
  pink logo → **language** [`i18n/`: English is the source, JA/ES fully translated, other
  offered languages fall back to the English UI but still set the language Moko *converses*
  in] → consent-gated **Terms** [the four safety floors, scroll-to-accept, decline exits] →
  name → **country→timezone** [bundled `data/country_zones.json` from `zone.tab`; single-zone
  countries auto-select, the ~30 multi-zone ones get a searchable city sub-pick with live
  local time] → rhythm/interests → brain → per-sense steps → channels [Telegram live, others
  shown "coming soon"]; a breadcrumb stepper across the top; Telegram pairing + Grok OAuth +
  the service offer run on the bare console as the frame tears down; `config.language` added;
  `test_setup.py`/`test_i18n.py`/`test_regions.py`/`test_catalog.py`). See ROADMAP M10.
- **M11 (Moko's world):** ⏸ **visual world retired** (see `WORLD.md`). Image gen is three
  plain modes. **Kept:** ✅ two-worlds graph split (`worlds` + `world_id` on subjects,
  world-scoped resolution/merges; `test_worlds.py`) · ✅ MediaStudio extraction · ✅ ambient
  weather · ✅ Token Tracker / Usage tab.

## M12 — The Burrow (runtime containment) 🔄 *(core built + tested; container run-through + setup UI remain — see `SANDBOX.md`)*

A ladder of runtime tiers so the companion chooses how much of their machine
Moko can touch. Default = **hardened host**; container is opt-in. Honest
threat model: bounds host blast radius, does **not** seal egress or hide
secrets from Moko.

- ✅ **Dual-writer fix (landed first).** `_daemon` distinguishes an
  unreachable daemon from a slow one (OK/BUSY/UNREACHABLE); `try_command`
  returns None only when nothing's listening; `run_via_daemon_or`/`outreach`/
  `snapshot` refuse the in-process fallback in a container tier; a stale
  socket is reaped, not read as "no daemon". (Fixed a latent host timeout-dup
  bug too.) `test_runtime_dispatch.py`.
- ✅ **`runtime.*` config.** `RuntimeConfig` (`mode` host|docker|podman,
  `hardened` off|on|strict, `image`, `container_name`, `monitor_port`,
  `mounts`) round-tripped + defaulted safe; editable via `moko config
  runtime.*` (validated). ⬜ Offer the tier in the `moko setup` wizard + the
  config TUI (settable via CLI today; the full-screen pickers are not wired).
- ✅ **Schema-version guard.** `store/db.py` raises `SchemaTooNewError` when a
  DB's applied migration exceeds this build's — so the editable host CLI and
  the built image can't migrate the mind out from under each other.
  `test_schema_guard.py`.
- ✅ **`Runtime` abstraction.** `moko/runtime/` (base + host + get_runtime);
  `HostRuntime` is the relocated `systemd --user` behaviour; `cli/service.py`
  is a thin facade dispatching through `get_runtime(cfg)`; existing host
  installs unchanged (`test_service.py`).
- ✅ **Hardened-host tier.** `runtime/hardening.py` splices a conservative
  systemd sandbox profile into the unit behind `runtime.hardened`; `on` keeps
  `$HOME` reachable, `strict` hides it (ReadWritePaths = `~/.moko` + mounts);
  `moko doctor` reports the tier. (Directives degrade safely on old systemd;
  kernel-effective reporting via `systemd-analyze security` is a nice-to-have,
  not yet done.)
- ✅ **Container tier (code-complete; engine run exercised manually).**
  `apps/agent/Dockerfile` (uv base, `--frozen`/`uv.lock`, ships the
  standalone `claude`); `ContainerRuntime` builds correct podman/docker argv
  (unit-tested), mounts `~/.moko` at the identical absolute path, publishes
  the monitor to host loopback, `--restart unless-stopped` for boot-persist
  (podman hint for `podman-restart.service`). Not yet run against a live
  Docker/Podman.
- ✅ **Claude-in-container auth.** `moko login` is runtime-aware — execs
  `claude` inside the container so the sign-in lands in the bind-mounted
  `~/.moko/claude` (→ container `~/.claude`); host mode unchanged. (Interactive
  flow exercised manually.)
- ✅ **Platform scope.** Linux gets the full ladder; the container mounts the
  mind at a real host path (never a VM-crossing bind mount) — mac/Windows are
  documented as hardened-host, container-for-the-mind deferred (SQLite
  corruption risk). See `SANDBOX.md`.
- ⬜ **Monitor self-status in-container.** Inside the container the engine
  binary is absent, so the dashboard's Service card reads "not available"
  (cosmetic). Give the in-container daemon a truthful self-report.

## M13 — The World (spatial consistency) — retired

⏸ **Retired.** Visual world-building is gone; image gen is three plain modes;
the two-worlds graph split remains. Record: `WORLD.md` / ROADMAP M11. No open
work here.

## Cross-cutting (revisit every milestone)
- ⬜ Honesty boundary holds (deflect-not-lie) — add a test per surface
  that can speak.
- ⬜ **Temporal awareness everywhere:** everything time-stamped; UTC
  storage + configured-tz rendering; one "now" source.
- ⬜ **No companion-specifics in code/permanent rules** (timezone,
  identity, channels) — all config, learned at setup.
- ⬜ Sentiment captured wherever speech is recorded.
- ⬜ Nothing is ever truly forgotten (decay, never delete).
- ⬜ Secrets only under `~/.moko/`, never in tree.
- ⬜ No API blasting; throttling on every bulk path.
- ⬜ `CHECKLIST.md` + `ROADMAP.md` kept honest.
