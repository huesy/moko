import { useEffect, useState } from "react";
import type { Rhythm } from "../types.ts";
import { usePolledFetch } from "../usePolledFetch.ts";

const ICON: Record<string, string> = {
  settle: "🫧",
  reflection: "💭",
  sleeping: "🌙",
  outreach: "📣",
  research: "🔎",
};

// State → text colour. "Eligible/queued" read as ready; cooling counts down;
// quiet/done/scheduled are calm; ceiling is a soft caution.
const COLOR: Record<string, string> = {
  eligible: "var(--good)",
  queued: "var(--c-outreach)",
  cooling: "var(--pink)",
  scheduled: "var(--muted)",
  quiet_hours: "var(--c-dream)",
  done_tonight: "var(--c-dream)",
  resting: "var(--c-dream)",
  ceiling: "var(--c-approval)",
  idle: "var(--muted-2)",
  disabled: "var(--muted-2)",
};

function countdown(next: string | null, now: number): string {
  if (!next) return "";
  const ms = new Date(next).getTime() - now;
  if (ms <= 0) return "now";
  const s = Math.floor(ms / 1000);
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const sec = s % 60;
  if (h > 0) return `${h}h ${m}m`;
  if (m > 0) return `${m}m ${sec}s`;
  return `${sec}s`;
}

// Moko's self-directed rhythms, as a rail card: each behavior with a soft countdown to
// when it's next eligible. (Eligible ≠ a prediction — Moko still decides if it's the
// moment.) Lives in the right rail; folds into a horizontal strip on narrow screens.
export function RhythmList() {
  const { data: rhythms } = usePolledFetch<Rhythm[]>("/api/rhythms", 20000); // re-sync; events shift next_at
  const [now, setNow] = useState(() => Date.now());

  // A 1s local tick drives the countdowns between the 20s re-syncs.
  useEffect(() => {
    const tick = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(tick);
  }, []);

  if (!rhythms || rhythms.length === 0) return null;

  return (
    <section
      className="rail-card rhythms"
      title="Earliest each self-directed behavior is eligible — not a prediction; Moko still decides if it's the moment."
    >
      <h4 className="rail-h">Rhythms</h4>
      <ul className="rhythm-list">
        {rhythms.map((r) => {
          const cd = countdown(r.next_at, now);
          return (
            <li className="rhythm" key={r.name} title={r.detail}>
              <span className="r-icon">{ICON[r.name] ?? "•"}</span>
              <span className="r-name">{r.name}</span>
              <span className="r-state" style={{ color: COLOR[r.state] ?? "var(--muted)" }}>
                {cd ? `~${cd}` : r.state.replace(/_/g, " ")}
              </span>
            </li>
          );
        })}
      </ul>
    </section>
  );
}
