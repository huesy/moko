import type { Dream } from "../types.ts";
import { ModalShell, fullTime } from "./ModalShell.tsx";

// Friendly labels for each consolidation step's count. Unknown keys keep their
// raw name, so a new dream stat still shows up (just unlabelled).
const STAT_LABELS: Record<string, string> = {
  thoughts_faded: "Thoughts faded",
  gaps_rescored: "Curiosities re-scored",
  merged: "Subjects merged",
  rejected: "Merges rejected",
  deferred: "Merges deferred",
  reclassified: "Values reclassified to links",
  contradictions: "Changes flagged to verify",
  gaps_deduped: "Duplicate curiosities merged",
  gaps_answered: "Curiosities resolved",
  rels_tidied: "Relationships tidied",
};

function fmtDuration(secs: number | null): string {
  if (secs == null) return "";
  if (secs < 60) return `${secs.toFixed(1)}s`;
  const m = Math.floor(secs / 60);
  return `${m}m ${Math.round(secs - m * 60)}s`;
}

// The full record of one night's consolidation: the prose summary, Moko's
// private reflection, every step's count, and how long it took.
export function DreamModal({ dream, onClose }: { dream: Dream; onClose: () => void }) {
  // Only the steps that actually did something — "what happened", not a wall of 0s.
  const active = Object.entries(dream.stats ?? {}).filter(([, n]) => n);

  return (
    <ModalShell
      icon="🌙"
      cls="ev-dream"
      title="dream"
      chip="consolidation"
      time={fullTime(dream.started_at)}
      onClose={onClose}
    >
      {dream.summary && <div className="modal-detail">{dream.summary}</div>}

      {dream.wonders && (
        <div className="dream-wonders">
          <div className="dream-wonders-label">Moko wondered…</div>
          <p>{dream.wonders}</p>
        </div>
      )}

      <div className="dream-stats">
        {active.length === 0 ? (
          <p className="dream-quiet">A quiet night — nothing needed tidying. 🌙</p>
        ) : (
          active.map(([k, n]) => (
            <div className="dream-stat" key={k}>
              <span className="dream-stat-n">{n}</span>
              <span className="dream-stat-k">{STAT_LABELS[k] ?? k}</span>
            </div>
          ))
        )}
      </div>

      <dl className="modal-fields">
        <div className="modal-field">
          <dt>Ran</dt>
          <dd>{fullTime(dream.started_at)}</dd>
        </div>
        {dream.ended_at && (
          <div className="modal-field">
            <dt>Finished</dt>
            <dd>{fullTime(dream.ended_at)}</dd>
          </div>
        )}
        {dream.duration_seconds != null && (
          <div className="modal-field">
            <dt>Duration</dt>
            <dd>{fmtDuration(dream.duration_seconds)}</dd>
          </div>
        )}
      </dl>
    </ModalShell>
  );
}
