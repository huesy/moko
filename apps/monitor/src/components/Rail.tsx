import type { Status } from "../types.ts";
import { RhythmList } from "./RhythmBar.tsx";

// The right rail — a calm "at a glance" sidebar that soaks up the width a wide window
// gives us, and in doing so keeps the masthead and feed uncluttered. Three soft cards:
// Moko's living rhythms, the operational vitals, and the size of the mind. On a narrow
// screen it folds down to just the rhythms strip (see styles.css).
export function Rail({ status }: { status: Status | null }) {
  const service = status?.service;
  const serviceText = !service?.available ? "—" : service.active ? "running" : "stopped";
  const sub = status?.subscription;
  const subText = !sub
    ? null
    : !sub.signed_in
      ? "signed out"
      : sub.hours_left != null
        ? `~${Math.round(sub.hours_left)}h left`
        : "signed in";
  const counts = status?.counts;

  return (
    <aside className="rail">
      <RhythmList />

      {status && (
        <section className="rail-card rail-glance">
          <h4 className="rail-h">At a glance</h4>
          <dl className="glance">
            <dt>service</dt>
            <dd className={`svc ${service?.active ? "on" : ""}`}>{serviceText}</dd>
            {subText && (
              <>
                <dt>subscription</dt>
                <dd>{subText}</dd>
              </>
            )}
            <dt>watching</dt>
            <dd>{status.observers}</dd>
            <dt>version</dt>
            <dd>v{status.moko_version}</dd>
          </dl>
        </section>
      )}

      {counts && Object.keys(counts).length > 0 && (
        <section className="rail-card rail-mind">
          <h4 className="rail-h">The mind</h4>
          <ul className="mind-counts">
            {Object.entries(counts).map(([k, v]) => (
              <li key={k}>
                <span className="mc-n">{v}</span>
                <span className="mc-k">{k}</span>
              </li>
            ))}
          </ul>
        </section>
      )}
    </aside>
  );
}
