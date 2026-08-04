# ROADMAP.md — Moko

Milestone-based, dependency-ordered. Each milestone is a **usable
increment**: at the end of it, Moko can do something it couldn't before.
We don't move on until the current one is real (built, tested, honest).

This is the **high-level** map — one block per milestone: what it is, why,
and where it stands. **`CHECKLIST.md` is the detailed, per-item tracker** —
read it when you want to know exactly what landed. `MISSION.md` is the
referee; where this roadmap and the mission ever disagree, the mission wins.

Legend: ✅ shipped + tested · 🔄 in progress · ⬜ planned · ⏸ parked

---

## M0 — Foundations ✅
**Goal:** a repo, a plan, and a skeleton that runs — config + `~/.moko/`
bootstrap (`moko setup`, capturing timezone + sleep window with nothing baked
into code), a UTC-storage / configured-tz clock, and a harmless `moko` CLI.
**Status:** shipped + tested.

## M1 — The Mind (memory graph) ✅
**Goal:** store and recall structured, sourced, time-aware knowledge — the
spine everything else hangs off. Typed values, numeric confidence,
supersede-not-delete, FTS5 recall, write-time subject resolution.
**Status:** shipped + tested. Facts enter with provenance + timestamps and come
back by query; the store carries real coverage.

## M2 — The Brain (agent loop + LLM) ✅
**Goal:** hold a conversation grounded in the graph. Provider-agnostic `llm/`
(Anthropic + OpenAI-compatible / xAI, plus a Claude *subscription* path using
the Claude Code OAuth token against the Messages API), perceive → respond →
extract, session rotation after consolidation.
**Status:** shipped + tested. *Residual:* sentiment on inbound messages is
**opportunistic** (tagged when the model includes it), not yet a guaranteed
per-message pass — tracked in CHECKLIST.

## M3 — The Voice (always-on runtime + Telegram) ✅
**Goal:** Moko lives where the companion is — a resilient `systemd --user`
daemon, not a request/response function. Single-queue / single-writer loop, a
Telegram channel (long-poll, inline approval buttons), code-secured pairing, a
~30s heartbeat, and operator tooling (`status` / `doctor` / `groom` / `journal`).
**Status:** shipped + tested. *Residual:* live-run verification against a real
bot token — tracked in CHECKLIST.

## M4 — The Spark (curiosity) ✅
**Goal:** Moko asks its own questions — *what*, *why*, and *is that true* — and
answers what it can itself. Gap detection + salience + `resolution_path`,
contradiction handling (supersede-with-history), ask-in-conversation.
**Status:** shipped + tested. Reactive research (looking things up mid-turn) is
always on; **unprompted** autonomous research is **opt-in**
(`curiosity.autonomous_research`, default off — it pulls untrusted web content
into the graph, so it's off until the companion enables it).

## M5 — The Heartbeat's decisions (proactivity, governed) ✅
**Goal:** reach out moderately, never spammily — what the M3 heartbeat decides
on each beat. Code-enforced **fuses** (sleep window, daily ceiling, settle
floor) with brain-judged timing *within* them; due commitments; and idle
reflection that forms private thoughts but never messages.
**Status:** shipped + tested. *Residual:* broad **pattern-notices** ("you've
cancelled three runs this month") not yet built.

## M6 — The Dream (nightly consolidation) ✅
**Goal:** while the companion rests, Moko consolidates instead of idling —
scheduled to the sleep window; subject reconciliation, thought/gap decay, LLM
cleanup, session-end, and an auditable dream journal.
**Status:** shipped + tested. *Residual:* daytime-pre-authorized code/system
*upgrades* executed in the dream are deferred (the `approvals` table exists;
`moko groom` does manual maintenance for now).

## M7 — Hands (acting in the world) 🔄
**Goal:** get things done, safely. A risk-classed tool layer (web, filesystem,
`execute_command`), memory reads **and** writes as tools folded into the reply
loop, inner-life continuity (`reflect`), and the consent gate with per-category
`moko trust`.
**Status:** the tool spine, memory-as-tools, and the consent gate are
**shipped + tested**. **Left:** Gmail / Calendar / Drive integration (reusing
the companion's tokens) — the main remaining M7 work.
**Done when:** "email my dentist to reschedule" works end-to-end, with the
consent gate firing on the send.

## M8 — Growth (self-extension) 🔄
**Goal:** Moko expands its own edges. A skill format + loader (built-in +
`~/.moko/skills/`, injected into the cached prefix), the core abilities living
as editable skills, and Moko **authoring + registering its own skills**.
**Status:** the loader, core-abilities-as-skills, and **`author_skill`** (Moko
writes + registers a gated skill, live from the next session) are
**shipped + tested**, along with the `integrate-service` meta-skill. **Left:**
skill **ranking** + `when:` gating (for when skills outgrow the prefix budget),
the **autonomous** "I keep hitting this — let me write a skill" trigger (today
it's companion-prompted), and a starter-skill set + `moko skill new` scaffold.
**Done when:** Moko notices a recurring need on its own, writes a skill for it,
and uses it.

## M9 — Delegation (sub-agents) ⬜ *(planned — see `SUBAGENTS.md`)*
**Goal:** spawn focused workers for big or long-running tasks, coordinate them
privately, and consolidate their results — without ever risking the mind. The
load-bearing rule: **a sub-agent never writes the graph** — it acts on the
*world* (gated) and reports back; only Moko consolidates into memory.
**Status:** planned.
**Done when:** "build me a small website" spins up a worker that does it
end-to-end, asks Moko for what it needs, reports back, and Moko persists what's
worth keeping — with the graph written only by Moko.

## M10 — Senses (multimodal) 🔄
**Goal:** a body's senses — exchange files, *see*, *hear*, *speak*, and show its
own face, without loosening the floors. Two reused seams: a `media/` blob +
attachment layer (every artifact sourced; EXIF scrubbed on send) and a
provider-agnostic capability seam (image / video / TTS / ASR, each configured
independently of the chat model).
**Status:** shipped + tested — vision, image generation (OpenAI / xAI Grok /
Google Gemini), video (xAI), voice (TTS + ASR), and Moko's phone-camera
pictures (`self_portrait`, `share_photo`, and the proactive `show`). One Grok
subscription can power the brain *and* the senses. **Left (minor):** a
`models.vision` override (a different model for sight than the brain) and
optional **local** ASR/TTS engines (whisper.cpp / Piper) for the
data-stays-local floor.
**Done when:** already met for the core — the companion sends a photo and Moko
understands it; "show me what you're doing" returns a portrait of Moko in its
current mood; every artifact sourced, every send gated where the floors say so.

## M11 — Moko's world ⏸ *(visual world retired; graph split kept)*
A persistent visual home for Moko was **retired** — consistency never landed
on a 2D generative stack, and the complexity wasn't worth it. Image gen is
three plain modes (selfie / photo Moko takes / general image). **Kept:** the
two-worlds graph split (`world_id` on subjects) so companion and Moko worlds
stay distinct. Short record: `WORLD.md`.

## M12 — The Burrow (runtime containment) 🔄 *(core built + tested — see `SANDBOX.md`)*
**Goal:** the companion chooses how contained Moko is — a ladder of runtime
tiers from a bare host process, to a hardened `systemd` unit (the default), to a
full Podman/Docker container. Defense in depth *beneath* the consent gate and
honesty floors, never a replacement for them.
**Status:** the **`Runtime` abstraction** (host + container + `get_runtime`,
with `service`/`start`/`login` dispatching through it), the **hardened-host
tier**, the **container tier** (code-complete — Dockerfile, the mind bind-mounted
at an identical path, Claude-in-container login), the `runtime.*` config, and
the schema-version guard are **built + tested**. **Left:** offering the tier in
the `moko setup` wizard + config TUI (settable via `moko config` today), and a
truthful in-container monitor self-status.
**Done when:** `moko setup` offers the tier; `moko service start` with
`runtime.mode=podman` brings up the container while `moko dream` on the host
signals *it* (not a second mind); the hardened unit measurably restricts a
runaway `execute_command`.

## Later / maybe
- ✅ *Shipped:* a local **monitor** (`apps/monitor`) — a live event feed, the
  brain browser (thoughts / subjects / gaps / commitments / outreach /
  messages), a dedicated **Dreams** audit screen, the rhythm bar, usage
  metering, and access tiers, served by the agent on `127.0.0.1` over an
  observer event bus.
- Honesty-boundary refinement (deflection that feels graceful, not evasive).
- Additional channels (Discord, email-as-channel, voice).
- Periodic re-confirmation of stale facts (curiosity-driven) — note:
  prudence/confidence *decay* lives in M6 dream; we never *forget*.

## Principles that survive the roadmap
- **The mind first.** Memory is the moat; build it right before anything flashy.
- **Each milestone is honest.** "Done" means tested and true, not demoed once.
- **Governors are not optional.** Curiosity and proactivity ship *with* their
  limits, never before them.
