import type { MokoEvent } from "../types.ts";
import { look } from "./Feed.tsx";
import { ModalShell, fmtValue, fullTime } from "./ModalShell.tsx";

// Friendly labels for the structured fields we know. Anything not listed still
// shows (the bus is an open contract) — it just keeps its raw key.
const FIELD_LABELS: Record<string, string> = {
  act: "Decision",
  reason: "Reasoning",
  candidate: "Candidate considered",
  reconsider_in_minutes: "Revisit in (minutes)",
  description: "Curiosity",
  resolution_path: "Resolution path",
  salience: "Salience",
  category: "Tool category",
  verdict: "Verdict",
  tool: "Tool",
  args: "Arguments",
  result: "Result",
  channel: "Channel",
  direction: "Direction",
  kind: "Kind",
  shareability: "Shareability",
  valence: "Valence",
  mode: "Mode",
  recorded: "Recorded",
  input_tokens: "Input tokens",
  output_tokens: "Output tokens",
  gap: "Gap",
  action: "Action",
  sentiment: "Perceived sentiment",
  stats: "Consolidation",
  body: "Body",
  text: "Text",
};

// Keys handled by the header/detail already, given a dedicated panel, or pure
// plumbing — don't repeat them in the generic field list.
const SKIP = new Set(["type", "at", "_k", "prompt"]);

// The exact prompt an LLM decision was made from, when an event carries one
// (today: outreach_consult). Shown raw and monospace so you can read — and tune —
// the real input. Each half is collapsible and height-capped so the system
// prompt doesn't swallow the modal; it rides on the event and is never persisted.
function PromptPanel({ prompt }: { prompt: { system?: string; user?: string } }) {
  const sections: [string, string | undefined][] = [
    ["System", prompt.system],
    ["User turn", prompt.user],
  ];
  if (!sections.some(([, body]) => body)) return null;
  return (
    <div className="modal-prompt">
      <div className="modal-prompt-label">Prompt sent to the LLM</div>
      {sections.map(([label, body]) =>
        body ? (
          <details className="prompt-sec" key={label} open>
            <summary>
              {label}
              <span className="prompt-len">{body.length.toLocaleString()} chars</span>
            </summary>
            <pre className="prompt-pre">{body}</pre>
          </details>
        ) : null,
      )}
    </div>
  );
}

export function EventModal({ event, onClose }: { event: MokoEvent; onClose: () => void }) {
  const l = look(event.type);
  const detail = l.detail(event);

  // The detail string is the human headline; avoid repeating whichever raw field
  // produced it verbatim in the table below.
  const fields = Object.entries(event).filter(
    ([k, v]) =>
      !SKIP.has(k) &&
      v !== null &&
      v !== undefined &&
      v !== "" &&
      fmtValue(v) !== detail,
  );

  return (
    <ModalShell
      icon={l.icon}
      cls={l.cls}
      title={l.title(event)}
      chip={l.group}
      time={fullTime(event.at)}
      onClose={onClose}
    >
      {detail && <div className="modal-detail">{detail}</div>}
      {fields.length > 0 && (
        <dl className="modal-fields">
          {fields.map(([k, v]) => (
            <div className="modal-field" key={k}>
              <dt>{FIELD_LABELS[k] ?? k}</dt>
              <dd>{fmtValue(v)}</dd>
            </div>
          ))}
        </dl>
      )}
      {event.prompt && <PromptPanel prompt={event.prompt} />}
    </ModalShell>
  );
}
