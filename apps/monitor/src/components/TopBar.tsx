import type { Status } from "../types.ts";

// The masthead: one calm line. Identity only (who Moko is + which brain), plus the
// live/​reconnecting pulse. The operational vitals (service, subscription, store sizes,
// rhythms) live in the right rail, not here — keeping the top from getting busy.
export function TopBar({ status, connected }: { status: Status | null; connected: boolean }) {
  const facts = status
    ? [status.companion, status.llm.model || status.llm.provider].filter(Boolean)
    : [];

  return (
    <header className="topbar">
      <div className="brand">
        <img src="/favicon.png" alt="Moko" className="brand-img" />
        <div className="brand-name">
          Moko <span className="bloom">monitor</span>
        </div>
        <div className={`conn ${connected ? "on" : "off"}`}>
          <span className="dot" />
          {connected ? "live" : "reconnecting…"}
        </div>
      </div>

      {facts.length > 0 && (
        <div className="topfacts">
          {facts.map((f, i) => (
            <span className="topfact" key={i}>
              {f}
            </span>
          ))}
        </div>
      )}
    </header>
  );
}
