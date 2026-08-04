import { useState } from "react";
import type { Usage, UsageKindRow, UsageRow, UsageSummary } from "../types.ts";
import { UsageDetailModal } from "./UsageDetailModal.tsx";
import { fullTime } from "./ModalShell.tsx";
import { usePolledFetch } from "../usePolledFetch.ts";

// How each activity shows up — a friendly label + icon for the raw `kind` the agent records.
const KINDS: Record<string, { icon: string; label: string }> = {
  reply: { icon: "💬", label: "reply" },
  vision: { icon: "👁", label: "image-viewing" },
  reflection: { icon: "🌷", label: "reflection" },
  dream: { icon: "🌙", label: "dream" },
  outreach: { icon: "📨", label: "outreach" },
  research: { icon: "🔎", label: "research" },
  other: { icon: "·", label: "other" },
};

const WINDOWS = [
  { key: "window_24h", label: "24h" },
  { key: "window_7d", label: "7 days" },
  { key: "all_time", label: "all time" },
] as const;
type WindowKey = (typeof WINDOWS)[number]["key"];

const MEDIA = { image: "🖼 images", audio: "🔊 voice notes", video: "🎬 videos" } as const;

const POLL_MS = 5000;

function fmt(n: number): string {
  return (n ?? 0).toLocaleString();
}

function kindMeta(kind: string) {
  return KINDS[kind] ?? { icon: "·", label: kind };
}

// Tokens served from cache vs. freshly read — the prompt-cache win, as a percentage.
function cacheHitRate(t: UsageSummary["totals"]): number | null {
  const denom = t.input_tokens + t.cache_read_tokens;
  return denom > 0 ? Math.round((t.cache_read_tokens / denom) * 100) : null;
}

export function UsageView() {
  const { data, error, reload } = usePolledFetch<Usage>("/api/usage?limit=100", POLL_MS);
  const [win, setWin] = useState<WindowKey>("window_24h");
  const [ioCall, setIoCall] = useState<string | null>(null); // a call id → its full I/O

  const summary: UsageSummary | null = data ? data[win] : null;
  const totals = summary?.totals;
  const hit = totals ? cacheHitRate(totals) : null;

  return (
    <section className="brain usage">
      <div className="brain-tabs">
        {WINDOWS.map((w) => (
          <button
            key={w.key}
            className={`btab ${win === w.key ? "on" : ""}`}
            onClick={() => setWin(w.key)}
          >
            {w.label}
          </button>
        ))}
        <button className="refresh" title="Refresh" onClick={reload}>
          ↻
        </button>
      </div>

      <div className="brain-body">
        {!data && !error && <p className="brain-empty">Loading…</p>}
        {error && !data && <p className="brain-empty">Couldn’t reach the agent. 🌸</p>}

        {totals && (
          <>
            <div className="usage-totals">
              <Big n={totals.input_tokens + totals.output_tokens} label="total tokens" />
              <Big n={totals.input_tokens} label="input" />
              <Big n={totals.output_tokens} label="output" />
              <Big n={totals.calls} label="calls" />
              {hit != null && <Big n={hit} label="cache hit" suffix="%" />}
            </div>

            <div className="section">
              <h3>By activity</h3>
              {summary!.by_kind.length === 0 ? (
                <p className="brain-empty small">No LLM calls in this window yet.</p>
              ) : (
                <ul className="cards">
                  {summary!.by_kind.map((k) => (
                    <KindCard key={k.kind} row={k} />
                  ))}
                </ul>
              )}
            </div>

            <div className="section">
              <h3>Media generated (all time)</h3>
              <div className="usage-media">
                {(Object.keys(MEDIA) as (keyof typeof MEDIA)[]).map((m) => (
                  <span key={m} className="badge soft">
                    {MEDIA[m]}: {data!.media[m] ?? 0}
                  </span>
                ))}
                <span className="usage-note">— priced per item, not by token</span>
              </div>
            </div>

            <div className="section">
              <h3>Recent calls</h3>
              {data!.rows.length === 0 ? (
                <p className="brain-empty small">none yet</p>
              ) : (
                <ul className="cards">
                  {data!.rows.map((row) => (
                    <RowCard key={row.id} row={row} onOpen={() => setIoCall(row.id)} />
                  ))}
                </ul>
              )}
            </div>
          </>
        )}
      </div>

      {ioCall && <UsageDetailModal id={ioCall} onClose={() => setIoCall(null)} />}
    </section>
  );
}

function Big({ n, label, suffix }: { n: number; label: string; suffix?: string }) {
  return (
    <div className="usage-big">
      <span className="usage-big-n">
        {fmt(n)}
        {suffix ?? ""}
      </span>
      <span className="usage-big-l">{label}</span>
    </div>
  );
}

function KindCard({ row }: { row: UsageKindRow }) {
  const m = kindMeta(row.kind);
  const total = row.input_tokens + row.output_tokens;
  return (
    <li className="card usage-kind">
      <div className="card-head">
        <span className="badge" title={row.kind}>
          {m.icon} {m.label}
        </span>
        <span className="card-meta">
          {fmt(total)} tokens · {fmt(row.calls)} call{row.calls === 1 ? "" : "s"}
        </span>
      </div>
      <p className="card-body usage-breakdown">
        in {fmt(row.input_tokens)} · out {fmt(row.output_tokens)}
        {row.cache_read_tokens > 0 && <> · cached {fmt(row.cache_read_tokens)}</>}
      </p>
    </li>
  );
}

function RowCard({ row, onOpen }: { row: UsageRow; onOpen: () => void }) {
  const m = kindMeta(row.kind);
  return (
    <li className="card usage-row clickable" onClick={onOpen} title="See the full I/O">
      <div className="card-head">
        <span className="badge" title={row.kind}>
          {m.icon} {m.label}
        </span>
        <span className="card-meta">
          in {fmt(row.input_tokens)} · out {fmt(row.output_tokens)} · {fullTime(row.created_at)}
        </span>
      </div>
    </li>
  );
}
