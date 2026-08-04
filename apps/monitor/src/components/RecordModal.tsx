import type { BrainMessage, Commitment, Gap, Thought } from "../types.ts";
import { ModalShell, fmtValue, fullTime } from "./ModalShell.tsx";

// The brain browser's record modal — the click-through detail for a single
// commitment or message. Subjects, thoughts, and gaps have their own richer,
// cross-linked DetailModal (relationships, related thoughts/curiosities), so they
// don't route here; this stays the flat field view for the rest.
export type BrainRecord = Thought | Gap | Commitment | BrainMessage;
export type RecordKind = "thoughts" | "gaps" | "commitments" | "messages";

interface Shape {
  icon: string;
  cls: string;
  title: (r: any) => string;
  body: (r: any) => string; // the headline text shown large
  time: (r: any) => string | null | undefined; // ISO timestamp for the header
}

// Friendlier labels for the field table; anything unlisted keeps its raw key.
const LABELS: Record<string, string> = {
  kind: "Kind",
  shareability: "Shareability",
  valence: "Valence (how Moko feels, −1…+1)",
  prudence: "Weight (prudence)",
  stability: "Resistance to fading (stability)",
  created_at: "Formed",
  resolution_path: "Resolution path",
  salience: "Salience",
  status: "Status",
  due_at: "Due",
  about_id: "About (subject id)",
  channel: "Channel",
  direction: "Direction",
  sentiment: "Perceived sentiment",
  sent_at: "Sent",
  id: "ID",
};

// Fields shown large in the header, so don't repeat them in the table below.
const SHAPES: Record<RecordKind, Shape> = {
  thoughts: {
    icon: "🌷",
    cls: "ev-thought",
    title: (t: Thought) => `thought · ${t.kind}`,
    body: (t: Thought) => t.body,
    time: (t: Thought) => t.created_at,
  },
  gaps: {
    icon: "❓",
    cls: "ev-thought",
    title: (g: Gap) => `curiosity · ${g.kind}`,
    body: (g: Gap) => g.description,
    time: (g: Gap) => g.opened_at,
  },
  commitments: {
    icon: "📌",
    cls: "ev-outreach",
    title: (c: Commitment) => `commitment · ${c.kind}`,
    body: (c: Commitment) => c.description,
    time: (c: Commitment) => c.created_at,
  },
  messages: {
    icon: "💬",
    cls: "ev-message",
    title: (m: BrainMessage) => (m.direction === "out" ? "Moko → reply" : "companion → message"),
    body: (m: BrainMessage) => m.body,
    time: (m: BrainMessage) => m.sent_at,
  },
};

const DATEISH = new Set(["created_at", "opened_at", "due_at", "sent_at", "completed_at"]);
const SKIP = new Set(["body", "description"]); // shown large in the header

export function RecordModal({
  kind,
  record,
  onClose,
}: {
  kind: RecordKind;
  record: BrainRecord;
  onClose: () => void;
}) {
  const s = SHAPES[kind];
  const body = s.body(record);

  const fields = Object.entries(record as unknown as Record<string, unknown>).filter(
    ([k, v]) => !SKIP.has(k) && v !== null && v !== undefined && v !== "",
  );

  return (
    <ModalShell
      icon={s.icon}
      cls={s.cls}
      title={s.title(record)}
      chip={kind.slice(0, -1)}
      time={fullTime(s.time(record))}
      onClose={onClose}
    >
      {body && <div className="modal-detail">{body}</div>}
      {fields.length > 0 && (
        <dl className="modal-fields">
          {fields.map(([k, v]) => (
            <div className="modal-field" key={k}>
              <dt>{LABELS[k] ?? k}</dt>
              <dd>{DATEISH.has(k) ? fullTime(v as string) : fmtValue(v)}</dd>
            </div>
          ))}
        </dl>
      )}
    </ModalShell>
  );
}
