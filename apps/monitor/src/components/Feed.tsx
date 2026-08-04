import { useEffect, useMemo, useState } from "react";
import type { MokoEvent } from "../types.ts";
import { EventModal } from "./EventModal.tsx";
import { UsageDetailModal } from "./UsageDetailModal.tsx";

// How each event type is presented. The bus is open, so unknown types still
// render via the fallback — they just use a neutral style.
export interface Look {
  group: string; // the filter bucket
  cls: string; // css accent class
  icon: string;
  title: (e: MokoEvent) => string;
  detail: (e: MokoEvent) => string | null;
}

const LOOKS: Record<string, Look> = {
  message: {
    group: "messages",
    cls: "ev-message",
    icon: "💬",
    title: (e) => (e.direction === "out" ? "Moko → reply" : "companion → message"),
    detail: (e) => e.body ?? null,
  },
  thought: {
    group: "thoughts",
    cls: "ev-thought",
    icon: "🌷",
    title: (e) => `thought · ${e.kind ?? ""} ${e.shareability === "private" ? "🔒" : ""}`.trim(),
    detail: (e) => e.body ?? null,
  },
  tool: {
    group: "tools",
    cls: "ev-tool",
    icon: "🔧",
    title: (e) => `tool · ${e.tool ?? "?"}`,
    detail: (e) => {
      const args = e.args ? JSON.stringify(e.args) : "";
      const res = e.result ? ` → ${String(e.result)}` : "";
      return `${args}${res}`.trim() || null;
    },
  },
  "turn.start": {
    group: "turns",
    cls: "ev-turn",
    icon: "▶",
    title: () => "turn started",
    detail: (e) => (e.text ? String(e.text) : null),
  },
  "turn.end": {
    group: "turns",
    cls: "ev-turn",
    icon: "■",
    title: (e) => `turn done${e.recorded ? " · remembered" : ""}`,
    detail: (e) =>
      e.input_tokens != null || e.output_tokens != null
        ? `tokens in ${e.input_tokens ?? "?"} / out ${e.output_tokens ?? "?"}`
        : null,
  },
  outreach: {
    group: "outreach",
    cls: "ev-outreach",
    icon: "📣",
    title: (e) => `outreach · ${e.kind ?? ""}`.trim(),
    detail: (e) => e.text ?? null,
  },
  research: {
    group: "outreach",
    cls: "ev-outreach",
    icon: "🔎",
    title: () => "researching a gap",
    detail: (e) => e.gap ?? null,
  },
  reflection: {
    group: "thoughts",
    cls: "ev-thought",
    icon: "💭",
    title: (e) => `reflection${e.mode ? ` · ${e.mode}` : ""}`,
    detail: (e) =>
      e.recorded ? "a thought or two formed" : "mind wandered — nothing landed",
  },
  outreach_consult: {
    group: "outreach",
    cls: "ev-outreach",
    icon: "🤔",
    title: (e) => `considered reaching out → ${e.act ? "yes" : "waiting"}`,
    detail: (e) => {
      const reason = e.reason ?? "";
      const wait =
        !e.act && e.reconsider_in_minutes != null
          ? `will revisit in ~${e.reconsider_in_minutes}m`
          : "";
      return [reason, wait].filter(Boolean).join(" · ") || null;
    },
  },
  "gap.opened": {
    group: "thoughts",
    cls: "ev-thought",
    icon: "❓",
    title: (e) => `got curious${e.resolution_path ? ` · ${e.resolution_path}` : ""}`,
    detail: (e) => e.description ?? null,
  },
  "gap.resolved": {
    group: "thoughts",
    cls: "ev-thought",
    icon: "💡",
    title: () => "curiosity satisfied",
    detail: (e) => e.description ?? null,
  },
  gate: {
    group: "approvals",
    cls: "ev-approval",
    icon: "🔓",
    title: (e) => `${e.verdict ?? "gated"} · ${e.tool ?? ""}`.trim(),
    detail: (e) => (e.category ? `category: ${e.category}` : null),
  },
  "process.start": {
    group: "processes",
    cls: "ev-tool",
    icon: "🟢",
    title: (e) => `started · ${e.name ?? "process"}`,
    detail: (e) => (e.command ? String(e.command) : null),
  },
  "process.exit": {
    group: "processes",
    cls: "ev-tool",
    icon: "⚫",
    title: (e) => `exited · ${e.name ?? "process"}`,
    detail: (e) => (e.code != null ? `exit code ${e.code}` : null),
  },
  "process.stop": {
    group: "processes",
    cls: "ev-tool",
    icon: "🛑",
    title: (e) => `stopped · ${e.name ?? "process"}`,
    detail: () => null,
  },
  "dream.start": {
    group: "dreams",
    cls: "ev-dream",
    icon: "🌙",
    title: () => "dreaming…",
    detail: () => null,
  },
  "dream.end": {
    group: "dreams",
    cls: "ev-dream",
    icon: "🌙",
    title: () => "dream finished",
    detail: (e) => {
      const s = e.stats;
      if (!s) return null;
      const parts: string[] = [];
      const add = (n: number | undefined, label: string) => {
        if (n) parts.push(`${n} ${label}`);
      };
      add(s.thoughts_faded, "faded");
      add(s.merged, "merged");
      add(s.gaps_answered, "gaps closed");
      add(s.gaps_deduped, "gaps merged");
      add(s.reclassified, "reclassified");
      add(s.rels_tidied, "edges tidied");
      return parts.length ? parts.join(" · ") : "nothing needed tidying";
    },
  },
  "approval.request": {
    group: "approvals",
    cls: "ev-approval",
    icon: "⚠️",
    title: () => "awaiting approval",
    detail: (e) => e.action ?? null,
  },
  "approval.resolved": {
    group: "approvals",
    cls: "ev-approval",
    icon: "✓",
    title: (e) => `approval · ${e.verdict ?? ""}`.trim(),
    detail: () => null,
  },
  heartbeat: {
    group: "heartbeat",
    cls: "ev-heartbeat",
    icon: "·",
    title: () => "heartbeat",
    detail: () => null,
  },
  "llm.call": {
    group: "llm",
    cls: "ev-tool",
    icon: "🧠",
    // Fresh input is the full-price part; cached reads ride at ~10% — showing only their
    // sum made turns look far pricier than they are.
    title: (e) => {
      const cached = e.cache_read_tokens ? ` (+${e.cache_read_tokens} cached)` : "";
      return `LLM · ${e.kind ?? "?"} · ${e.input_tokens ?? "?"}${cached}→${e.output_tokens ?? "?"} tok`;
    },
    detail: (e) => (typeof e.preview === "string" ? e.preview : null),
  },
  "media.generated": {
    group: "media",
    cls: "ev-tool",
    icon: "🎨",
    title: (e) => `Moko made ${mediaNoun(e)}`,
    detail: (e) => (typeof e.gen_prompt === "string" ? e.gen_prompt : null), // the exact prompt
  },
  "media.sent": {
    group: "media",
    cls: "ev-out",
    icon: "📤",
    title: (e) => `Moko → sent ${mediaNoun(e)}`,
    detail: (e) => mediaCaption(e),
  },
  "media.received": {
    group: "media",
    cls: "ev-message",
    icon: "📥",
    title: (e) => `companion → sent ${mediaNoun(e)}`,
    detail: (e) => mediaCaption(e),
  },
  "turn.error": {
    group: "turns",
    cls: "ev-approval",
    icon: "⚠️",
    title: () => "turn failed",
    detail: (e) => (typeof e.channel === "string" ? `on ${e.channel}` : null),
  },
  snapshot: {
    group: "other",
    cls: "ev-other",
    icon: "💾",
    title: (e) => `db snapshot · ${e.name ?? ""}`,
    detail: (e) => (e.kb != null ? `${e.kb} KB` : null),
  },
  "commitment.created": {
    group: "commitments",
    cls: "ev-turn",
    icon: "📌",
    title: (e) => `commitment · ${e.kind ?? ""}`.trim(),
    detail: (e) => (typeof e.description === "string" ? e.description : null),
  },
  "commitment.completed": {
    group: "commitments",
    cls: "ev-turn",
    icon: "✅",
    title: () => "commitment done",
    detail: (e) => (typeof e.description === "string" ? e.description : null),
  },
  learned: {
    group: "learned",
    cls: "ev-thought",
    icon: "📚",
    title: (e) => `learned · ${e.what ?? ""}`.trim(),
    detail: learnedDetail,
  },
  failure: {
    group: "failures",
    cls: "ev-approval",
    icon: "❌",
    title: (e) => `failed · ${e.what ?? ""}`.trim(),
    detail: (e) => (typeof e.reason === "string" ? e.reason : null),
  },
};

// What a `learned` event added to the graph — a subject, a fact, or a relationship.
function learnedDetail(e: MokoEvent): string | null {
  if (e.what === "subject") return `${e.kind ?? "?"}: ${e.name ?? ""}`;
  if (e.what === "fact") return `${e.key ?? "?"}: ${e.value ?? ""}`;
  if (e.what === "link") return e.rel_type ?? null;
  return null;
}

const FALLBACK: Look = {
  group: "other",
  cls: "ev-other",
  icon: "•",
  title: (e) => e.type,
  detail: (e) => {
    const { type, at, _k, ...rest } = e as Record<string, unknown>;
    void type;
    void at;
    void _k;
    const keys = Object.keys(rest);
    return keys.length ? JSON.stringify(rest, null, 2) : null;
  },
};

export function look(type: string): Look {
  return LOOKS[type] ?? FALLBACK;
}

// Media event helpers: a human noun for the kind, a caption fallback, and (for images) a
// thumbnail deep-link.
function mediaNoun(e: MokoEvent): string {
  const k = typeof e.media_kind === "string" ? e.media_kind : "";
  return k === "image" ? "a picture" : k === "video" ? "a video"
    : k === "audio" ? "a voice note" : k === "document" ? "a file" : "an attachment";
}
function mediaCaption(e: MokoEvent): string | null {
  if (typeof e.caption === "string" && e.caption) return e.caption;
  if (typeof e.filename === "string" && e.filename) return e.filename;
  return null;
}
function mediaThumb(e: MokoEvent): string | null {
  return e.media_kind === "image" && typeof e.id === "string"
    ? `/api/media/blob/${e.id}?thumb=1`
    : null;
}

// All filter groups, in display order — the fine-grained surface (the "Advanced" row).
const GROUPS = [
  "messages",
  "thoughts",
  "learned",
  "commitments",
  "media",
  "outreach",
  "dreams",
  "approvals",
  "failures",
  "turns",
  "tools",
  "llm",
  "processes",
  "other",
  "heartbeat",
];

// Friendly "lenses" bundle the groups into a few human categories, so the feed reads as
// a handful of toggles instead of a wall of 15. Every group belongs to exactly one lens,
// so the lens row and the Advanced row stay in sync. A lens is "on" when none of its
// groups are hidden, "off" when all are, "partial" in between.
interface Lens {
  key: string;
  label: string;
  icon: string;
  cls: string;
  groups: string[];
}
const LENSES: Lens[] = [
  { key: "conversation", label: "Conversation", icon: "💬", cls: "lens-conv",
    groups: ["messages", "media"] },
  { key: "inner", label: "Inner life", icon: "🌷", cls: "lens-inner",
    groups: ["thoughts", "learned", "dreams"] },
  { key: "doing", label: "Doing", icon: "📣", cls: "lens-doing",
    groups: ["outreach", "commitments", "approvals", "failures"] },
  { key: "system", label: "System", icon: "🔧", cls: "lens-system",
    groups: ["turns", "tools", "llm", "processes", "other", "heartbeat"] },
];
// The plumbing (System) starts hidden, so the first impression is a calm feed of the
// human-meaningful things: conversation, inner life, and what Moko is doing.
const DEFAULT_HIDDEN = LENSES.find((l) => l.key === "system")!.groups;

// Coarse, stable relative buckets for the day-dividers between rows. Coarse on purpose —
// a fresh "3m ago / 4m ago …" label every minute would scatter dividers everywhere.
function bucketOf(at: string | undefined, now: number): string {
  const t = at ? new Date(at).getTime() : NaN;
  if (Number.isNaN(t)) return "Earlier";
  const diff = now - t;
  if (diff < 120_000) return "Just now";
  if (diff < 3_600_000) return "Last hour";
  const day = new Date(t).toDateString();
  if (day === new Date(now).toDateString()) return "Earlier today";
  if (day === new Date(now - 86_400_000).toDateString()) return "Yesterday";
  return new Date(t).toLocaleDateString();
}

// Per-row stamp: time of day, no seconds — calmer than a ticking clock.
function hhmm(at: string | undefined): string {
  if (!at) return "";
  const d = new Date(at);
  return Number.isNaN(d.getTime())
    ? ""
    : d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

export function Feed({ events, onClear }: { events: MokoEvent[]; onClear: () => void }) {
  const [hidden, setHidden] = useState<Set<string>>(() => new Set(DEFAULT_HIDDEN));
  const [query, setQuery] = useState("");
  const [advanced, setAdvanced] = useState(false);
  const [selected, setSelected] = useState<MokoEvent | null>(null);
  const [ioCall, setIoCall] = useState<string | null>(null); // an llm.call id → full I/O modal
  const [now, setNow] = useState(() => Date.now());

  // A slow tick keeps the relative dividers ("Just now" → "Last hour") honest without
  // re-rendering on every second. Keyed rows aren't remounted, so this never re-animates.
  useEffect(() => {
    const tick = setInterval(() => setNow(Date.now()), 30_000);
    return () => clearInterval(tick);
  }, []);

  // An llm.call row opens the full system/request/reply I/O; anything else, the raw event.
  const open = (e: MokoEvent) =>
    e.type === "llm.call" && typeof e.id === "string" ? setIoCall(e.id) : setSelected(e);

  const toggleGroup = (g: string) =>
    setHidden((prev) => {
      const next = new Set(prev);
      next.has(g) ? next.delete(g) : next.add(g);
      return next;
    });

  const lensState = (l: Lens): "on" | "off" | "partial" => {
    const off = l.groups.filter((g) => hidden.has(g)).length;
    return off === 0 ? "on" : off === l.groups.length ? "off" : "partial";
  };

  const toggleLens = (l: Lens) =>
    setHidden((prev) => {
      const next = new Set(prev);
      const fullyOn = l.groups.every((g) => !next.has(g));
      // fully on → turn the whole lens off; otherwise (off/partial) → turn it fully on.
      l.groups.forEach((g) => (fullyOn ? next.add(g) : next.delete(g)));
      return next;
    });

  const shown = useMemo(() => {
    const q = query.trim().toLowerCase();
    return events.filter((e) => {
      const l = look(e.type);
      if (hidden.has(l.group)) return false;
      if (!q) return true;
      return `${l.title(e)} ${l.detail(e) ?? ""} ${e.type}`.toLowerCase().includes(q);
    });
  }, [events, hidden, query]);

  return (
    <section className="feed">
      <div className="feed-controls">
        <div className="lenses">
          {LENSES.map((l) => (
            <button
              key={l.key}
              className={`lens ${l.cls} ${lensState(l)}`}
              onClick={() => toggleLens(l)}
              title={`${l.label}: ${l.groups.join(", ")}`}
            >
              <span className="lens-ic">{l.icon}</span>
              {l.label}
            </button>
          ))}
          <button
            className={`lens-adv ${advanced ? "on" : ""}`}
            onClick={() => setAdvanced((a) => !a)}
            title="Toggle individual event types"
          >
            Advanced {advanced ? "▴" : "▾"}
          </button>
        </div>
        <div className="feed-meta">
          <input
            className="feed-search"
            type="search"
            placeholder="search the feed…"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
          />
          <span className="shown-n" title="events shown">
            {shown.length}
          </span>
          <button className="clear" onClick={onClear}>
            clear
          </button>
        </div>
      </div>

      {advanced && (
        <div className="chips-adv">
          {GROUPS.map((g) => (
            <button
              key={g}
              className={`chip ${hidden.has(g) ? "off" : "on"}`}
              onClick={() => toggleGroup(g)}
            >
              {g}
            </button>
          ))}
        </div>
      )}

      <ol className="events">
        {shown.length === 0 && (
          <li className="empty">
            {query.trim()
              ? "Nothing matches that search. 🌸"
              : "Waiting for Moko to do something… message it, or wait for a heartbeat. 🌸"}
          </li>
        )}
        {shown.map((e, i) => {
          const l = look(e.type);
          const detail = l.detail(e);
          const k = (e as { _k?: number })._k as number;
          // A soft date-divider whenever the relative bucket changes from the row above.
          const b = bucketOf(e.at, now);
          const divider = i === 0 || b !== bucketOf(shown[i - 1].at, now);
          const thumb = mediaThumb(e);
          return (
            <li key={k} className="event-wrap">
              {divider && <div className="day-divider">{b}</div>}
              {/* Every row opens the full record in a modal — even detail-less ones
                  carry structured fields worth seeing. */}
              <div className={`event ${l.cls} has-detail`} onClick={() => open(e)}>
                <span className="ev-time">{hhmm(e.at)}</span>
                <span className="ev-icon">{l.icon}</span>
                <div className="ev-body">
                  <div className="ev-title">
                    {l.title(e)}
                    <span className="ev-caret">⤢</span>
                  </div>
                  {detail && <div className="ev-detail">{detail}</div>}
                </div>
                {thumb && <img className="ev-thumb" src={thumb} alt="" loading="lazy" />}
              </div>
            </li>
          );
        })}
      </ol>

      {selected && <EventModal event={selected} onClose={() => setSelected(null)} />}
      {ioCall && <UsageDetailModal id={ioCall} onClose={() => setIoCall(null)} />}
    </section>
  );
}
