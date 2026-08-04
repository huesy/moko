import { useState } from "react";
import type { Dream } from "../types.ts";
import { DreamModal } from "./DreamModal.tsx";
import { fullTime } from "./ModalShell.tsx";
import { usePolledFetch } from "../usePolledFetch.ts";

const POLL_MS = 15000;

// The dream audit — every nightly consolidation, newest first. Each card opens the
// full record: the prose summary, Moko's private reflection, every step's count, and
// how long the night took. Dreams accrue one per night, so a generous page holds years.
export function DreamsView() {
  const [open, setOpen] = useState<Dream | null>(null);
  const { data, reload } = usePolledFetch<Dream[]>("/api/brain/dreams?limit=200", POLL_MS);
  const dreams = data ?? [];

  return (
    <section className="brain dreams">
      <div className="brain-tabs">
        <span className="btab on">
          🌙 dreams
          {dreams.length > 0 && <span className="btab-count">{dreams.length}</span>}
        </span>
        <button className="refresh" title="Refresh" onClick={reload}>
          ↻
        </button>
      </div>

      <div className="brain-body">
        {dreams.length === 0 ? (
          <p className="brain-empty">No dreams yet — Moko consolidates while you sleep. 🌙</p>
        ) : (
          <ul className="cards">
            {dreams.map((d) => (
              <li className="card dream clickable" key={d.id} onClick={() => setOpen(d)}>
                <div className="card-head">
                  <span className="badge">🌙 dream</span>
                  <span className="card-meta">{fullTime(d.started_at)}</span>
                </div>
                <p className="card-body">{d.summary || "a quiet consolidation"}</p>
                {d.wonders && <p className="dream-card-wonder">“{d.wonders}”</p>}
              </li>
            ))}
          </ul>
        )}
      </div>

      {open && <DreamModal dream={open} onClose={() => setOpen(null)} />}
    </section>
  );
}
