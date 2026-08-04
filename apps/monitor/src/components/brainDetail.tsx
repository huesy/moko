import type {
  Attribute,
  Gap,
  GapDetail,
  Relationship,
  SubjectDetail,
  SubjectRef,
  Thought,
  ThoughtDetail,
} from "../types.ts";
import { ModalShell, fullTime } from "./ModalShell.tsx";
import { type Detail, type DetailKind, valence } from "./brainShared.ts";

// The detail modal: a navigable popup for a subject / thought / gap. The header
// comes from the record kind; the body cross-links to related records, and each
// click pushes onto the trail so the companion drills in and steps back out. All
// three kinds share one shell, so they open, animate, and dismiss identically.
export function DetailModal({
  detail,
  depth,
  onOpen,
  onBack,
  onClose,
}: {
  detail: Detail;
  depth: number; // trail length — "back" only shows when there's somewhere to go
  onOpen: (kind: DetailKind, id: string) => void;
  onBack: () => void;
  onClose: () => void;
}) {
  const head =
    detail.kind === "thought"
      ? {
          icon: "🌷",
          cls: "ev-thought",
          title: `thought · ${detail.data.thought.kind}`,
          chip: "thought",
          time: fullTime(detail.data.thought.created_at),
        }
      : detail.kind === "gap"
        ? {
            icon: "❓",
            cls: "ev-thought",
            title: `curiosity · ${detail.data.gap.kind}`,
            chip: "curiosity",
            time: fullTime(detail.data.gap.opened_at),
          }
        : {
            icon: "🧩",
            cls: "ev-tool",
            title: `${detail.data.subject.kind} · ${detail.data.subject.name}`,
            chip: "subject",
            time: fullTime(detail.data.subject.created_at),
          };
  return (
    <ModalShell
      icon={head.icon}
      cls={head.cls}
      title={head.title}
      chip={head.chip}
      time={head.time}
      onClose={onClose}
    >
      {depth > 1 && (
        <button className="back" onClick={onBack}>
          ‹ back
        </button>
      )}
      {detail.kind === "thought" && <ThoughtBody detail={detail.data} onOpen={onOpen} />}
      {detail.kind === "gap" && <GapBody detail={detail.data} onOpen={onOpen} />}
      {detail.kind === "subject" && <SubjectBody detail={detail.data} onOpen={onOpen} />}
    </ModalShell>
  );
}

// A clickable curiosity mini-card, for the "related gaps" lists.
function GapMini({ g, onOpen }: { g: Gap; onOpen: (kind: DetailKind, id: string) => void }) {
  return (
    <li className="card gap clickable" onClick={() => onOpen("gap", g.id)}>
      <div className="card-head">
        <span className="badge">{g.kind}</span>
        <span className="badge soft">{g.status}</span>
      </div>
      <p className="card-body">{g.description}</p>
    </li>
  );
}

// A clickable thought mini-card, for the "related thoughts" lists.
function ThoughtMini({ t, onOpen }: { t: Thought; onOpen: (kind: DetailKind, id: string) => void }) {
  return (
    <li className="card thought clickable" onClick={() => onOpen("thought", t.id)}>
      <div className="card-head">
        <span className="badge">{t.kind}</span>
        <span className="card-meta">
          {valence(t.valence)} · {fullTime(t.created_at)}
        </span>
      </div>
      <p className="card-body">{t.body}</p>
    </li>
  );
}

// A clickable list of subject references — the "About" section of a thought/gap.
function RefList({
  refs,
  onOpen,
}: {
  refs: SubjectRef[];
  onOpen: (kind: DetailKind, id: string) => void;
}) {
  return (
    <ul className="rels">
      {refs.map((e) => (
        <li key={e.id} onClick={() => onOpen("subject", e.id)}>
          <span className="rel-type">{e.kind}</span>
          <span className="rel-other">{e.name}</span>
        </li>
      ))}
    </ul>
  );
}

function SubjectBody({
  detail,
  onOpen,
}: {
  detail: SubjectDetail;
  onOpen: (kind: DetailKind, id: string) => void;
}) {
  const { subject, attributes, relationships, thoughts, gaps } = detail;
  return (
    <>
      {subject.notes && <div className="modal-detail">{subject.notes}</div>}

      <Section title="Attributes" empty={attributes.length === 0}>
        <table className="attrs">
          <tbody>
            {attributes.map((a: Attribute) => (
              <tr key={a.id}>
                <td className="k">{a.key}</td>
                <td className="v">{a.value}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </Section>

      <Section title="Relationships" empty={relationships.length === 0}>
        <ul className="rels">
          {relationships.map((r: Relationship) => (
            <li key={r.id} onClick={() => onOpen("subject", r.other_id)}>
              <span className="rel-type">
                {r.direction === "out" ? "→" : "←"} {r.type}
              </span>
              <span className="rel-other">{r.other_name}</span>
            </li>
          ))}
        </ul>
      </Section>

      <Section title="Moko's thoughts about them" empty={thoughts.length === 0}>
        <ul className="cards">
          {thoughts.map((t: Thought) => (
            <ThoughtMini key={t.id} t={t} onOpen={onOpen} />
          ))}
        </ul>
      </Section>

      <Section title="Curiosities about them" empty={gaps.length === 0}>
        <ul className="cards">
          {gaps.map((g: Gap) => (
            <GapMini key={g.id} g={g} onOpen={onOpen} />
          ))}
        </ul>
      </Section>
    </>
  );
}

function ThoughtBody({
  detail,
  onOpen,
}: {
  detail: ThoughtDetail;
  onOpen: (kind: DetailKind, id: string) => void;
}) {
  const { thought, subjects, gaps } = detail;
  return (
    <>
      <div className="modal-detail">{thought.body}</div>
      <p className="card-meta">
        {valence(thought.valence)} · {thought.shareability}
        {thought.shareability === "private" && " 🔒"}
      </p>

      <Section title="About" empty={subjects.length === 0}>
        <RefList refs={subjects} onOpen={onOpen} />
      </Section>

      <Section title="Related curiosities" empty={gaps.length === 0}>
        <ul className="cards">
          {gaps.map((g) => (
            <GapMini key={g.id} g={g} onOpen={onOpen} />
          ))}
        </ul>
      </Section>
    </>
  );
}

function GapBody({
  detail,
  onOpen,
}: {
  detail: GapDetail;
  onOpen: (kind: DetailKind, id: string) => void;
}) {
  const { gap, subject, thoughts } = detail;
  return (
    <>
      <div className="modal-detail">{gap.description}</div>
      <p className="card-meta">
        {gap.resolution_path} · {gap.status} · salience{" "}
        {gap.salience?.toFixed?.(2) ?? gap.salience}
      </p>

      <Section title="About" empty={!subject}>
        {subject && <RefList refs={[subject]} onOpen={onOpen} />}
      </Section>

      <Section title="Thoughts about this" empty={thoughts.length === 0}>
        <ul className="cards">
          {thoughts.map((t) => (
            <ThoughtMini key={t.id} t={t} onOpen={onOpen} />
          ))}
        </ul>
      </Section>
    </>
  );
}

function Section({
  title,
  empty,
  children,
}: {
  title: string;
  empty: boolean;
  children: React.ReactNode;
}) {
  return (
    <div className="section">
      <h3>{title}</h3>
      {empty ? <p className="brain-empty small">none</p> : children}
    </div>
  );
}
