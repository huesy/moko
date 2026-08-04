import type { BrainMessage, Commitment, Gap, Subject, Thought } from "../types.ts";
import { fullTime } from "./ModalShell.tsx";
import type { BrainRecord, RecordKind } from "./RecordModal.tsx";
import { type DetailKind, type Tab, valence } from "./brainShared.ts";

// One row of the reaching-out history: either a message Moko sent, or a
// deliberation where it considered reaching out and (often) decided to wait.
type OutreachEntry = {
  kind: "sent" | "consult";
  at?: string | null;
  outreach_kind?: string;
  engaged?: number | null;
  acted?: boolean;
  candidate?: string | null;
  reasoning?: string | null;
  reconsider_minutes?: number | null;
};

// The per-tab list renderer. Each branch is the card layout for one tab; they're mutually
// exclusive on `tab`. The container owns paging/scroll/nav — this is pure presentation.
export function BrainList({
  tab,
  items,
  onOpenDetail,
  onOpenRecord,
}: {
  tab: Tab;
  items: unknown[];
  onOpenDetail: (kind: DetailKind, id: string) => void;
  onOpenRecord: (kind: RecordKind, data: BrainRecord) => void;
}) {
  if (tab === "outreach")
    return (
      <ul className="cards">
        {(items as OutreachEntry[]).map((o, i) => (
          <li className="card outreach" key={`${o.at}-${i}`}>
            {o.kind === "sent" ? (
              <>
                <div className="card-head">
                  <span className="badge">📨 reached out</span>
                  <span className="card-meta">
                    {o.outreach_kind} · {o.engaged ? "answered" : "no reply yet"} · {fullTime(o.at)}
                  </span>
                </div>
                <p className="card-body">Sent an unprompted {o.outreach_kind} message.</p>
              </>
            ) : (
              <>
                <div className="card-head">
                  <span className="badge soft">
                    {o.acted ? "💬 chose to reach out" : "🤔 considered, held off"}
                  </span>
                  <span className="card-meta">
                    {o.candidate ? `${o.candidate} · ` : ""}
                    {fullTime(o.at)}
                    {!o.acted && o.reconsider_minutes
                      ? ` · revisit ~${Math.round(o.reconsider_minutes)}m`
                      : ""}
                  </span>
                </div>
                {o.reasoning && <p className="card-body">{o.reasoning}</p>}
              </>
            )}
          </li>
        ))}
      </ul>
    );

  if (tab === "thoughts")
    return (
      <ul className="cards">
        {(items as Thought[]).map((t) => (
          <li
            className="card thought clickable"
            key={t.id}
            onClick={() => onOpenDetail("thought", t.id)}
          >
            <div className="card-head">
              <span
                className="badge"
                title="Kind of thought: opinion, feeling, curiosity, hypothesis, prediction, concern, or note"
              >
                {t.kind}
              </span>
              {t.shareability === "private" && (
                <span className="lock" title="Private — Moko keeps this to itself unless it chooses to share">
                  🔒 private
                </span>
              )}
              {t.world_id === "world-moko" && (
                <span className="badge soft world-moko" title="About Moko's own world">🏠</span>
              )}
              <span className="card-meta">
                <span title="How Moko feels about this thought (−1 troubled … 0 neutral … +1 fond). Strongly-felt thoughts fade more slowly.">
                  {valence(t.valence)}
                </span>
                {" · "}
                <span
                  title={`Formed ${fullTime(t.created_at)} · weight (prudence) ${t.prudence?.toFixed?.(2) ?? "?"}, resistance to fading (stability) ${t.stability?.toFixed?.(2) ?? "?"}`}
                >
                  {fullTime(t.created_at)}
                </span>
              </span>
            </div>
            <p className="card-body">{t.body}</p>
          </li>
        ))}
      </ul>
    );

  if (tab === "gaps")
    return (
      <ul className="cards">
        {(items as Gap[]).map((g) => (
          <li
            className={`card gap clickable${
              g.status === "resolved" || g.status === "abandoned" ? " done" : ""
            }`}
            key={g.id}
            onClick={() => onOpenDetail("gap", g.id)}
          >
            <div className="card-head">
              <span className="badge">{g.kind}</span>
              <span className="badge soft">{g.resolution_path}</span>
              {g.world_id === "world-moko" && (
                <span className="badge soft world-moko" title="About Moko's own world">🏠</span>
              )}
              <span className="card-meta">
                {g.status} · salience {g.salience?.toFixed?.(2) ?? g.salience}
              </span>
            </div>
            <p className="card-body">{g.description}</p>
          </li>
        ))}
      </ul>
    );

  if (tab === "commitments")
    return (
      <ul className="cards">
        {(items as Commitment[]).map((c) => (
          <li
            className={`card commit clickable${c.status !== "open" ? " done" : ""}`}
            key={c.id}
            onClick={() => onOpenRecord("commitments", c)}
          >
            <div className="card-head">
              <span className="badge">{c.kind}</span>
              <span className="card-meta">
                {c.due_at ? `due ${fullTime(c.due_at)}` : "no due date"} · {c.status}
              </span>
            </div>
            <p className="card-body">{c.description}</p>
          </li>
        ))}
      </ul>
    );

  if (tab === "messages")
    return (
      <ul className="cards">
        {(items as BrainMessage[]).map((m) => (
          <li
            className={`card msg ${m.direction} clickable`}
            key={m.id}
            onClick={() => onOpenRecord("messages", m)}
          >
            <div className="card-head">
              <span className="badge">{m.direction === "out" ? "Moko" : "companion"}</span>
              <span className="badge soft">{m.channel}</span>
              <span className="card-meta">
                {m.sentiment ? `${m.sentiment} · ` : ""}
                {fullTime(m.sent_at)}
              </span>
            </div>
            <p className="card-body">{m.body}</p>
          </li>
        ))}
      </ul>
    );

  // subjects
  return (
    <ul className="cards subjects">
      {(items as Subject[]).map((e) => (
        <li className="card subject-row" key={e.id} onClick={() => onOpenDetail("subject", e.id)}>
          <span className="badge">{e.kind}</span>
          <span className="subject-name">{e.name}</span>
          {e.world_id === "world-moko" && (
            <span className="badge soft world-moko" title="In Moko's own world">🏠 Moko</span>
          )}
          <span className="card-meta">conf {e.confidence?.toFixed?.(2) ?? e.confidence} ›</span>
        </li>
      ))}
    </ul>
  );
}
