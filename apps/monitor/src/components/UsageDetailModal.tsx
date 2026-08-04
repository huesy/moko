import { useEffect, useState } from "react";
import { ModalShell, fullTime } from "./ModalShell.tsx";

// The full I/O of one LLM call (M11): exactly what went to the model and came back. Fetched
// by id, so both the Usage tab and the live feed can open the same view.
interface UsageDetail {
  id: string;
  created_at: string;
  kind: string;
  model: string | null;
  input_tokens: number;
  output_tokens: number;
  cache_read_tokens: number;
  cache_write_tokens: number;
  system_prompt: string | null;
  request: string | null;
  response: string | null;
  tool_calls: string | null; // JSON of [{name, input}] — the model's structured output
}

// The structured half of the reply: every tool the model called, with its exact arguments
// (a `remember` payload, a `decide_outreach`). This is what reveals things like a commitment
// being re-recorded on an unrelated turn.
function ToolCallsView({ raw }: { raw: string | null }) {
  if (!raw) return <p className="brain-empty small">none</p>;
  let calls: { name?: string; input?: unknown }[] | null = null;
  try {
    const parsed = JSON.parse(raw);
    if (Array.isArray(parsed)) calls = parsed;
  } catch {
    /* not JSON — show raw */
  }
  if (!calls) return <pre className="io-pre">{raw}</pre>;
  return (
    <>
      {calls.map((c, i) => (
        <div key={i} className="io-msg">
          <span className="io-role">{c.name ?? "?"}</span>
          <pre className="io-pre">{JSON.stringify(c.input ?? {}, null, 2)}</pre>
        </div>
      ))}
    </>
  );
}

// The request is stored as JSON of [{role, text}]; render it as labelled turns, or fall
// back to the raw string if it isn't what we expect.
function RequestView({ raw }: { raw: string | null }) {
  if (!raw) return <p className="brain-empty small">none</p>;
  let msgs: { role?: string; text?: string }[] | null = null;
  try {
    const parsed = JSON.parse(raw);
    if (Array.isArray(parsed)) msgs = parsed;
  } catch {
    /* not JSON — show raw below */
  }
  if (!msgs) return <pre className="io-pre">{raw}</pre>;
  return (
    <>
      {msgs.map((m, i) => (
        <div key={i} className="io-msg">
          <span className="io-role">{m.role ?? "?"}</span>
          <pre className="io-pre">{m.text ?? ""}</pre>
        </div>
      ))}
    </>
  );
}

export function UsageDetailModal({ id, onClose }: { id: string; onClose: () => void }) {
  const [d, setD] = useState<UsageDetail | null>(null);
  const [missing, setMissing] = useState(false);

  useEffect(() => {
    let alive = true;
    fetch(`/api/usage/${id}`)
      .then((r) => (r.ok ? r.json() : null))
      .then((j) => alive && (j ? setD(j) : setMissing(true)))
      .catch(() => alive && setMissing(true));
    return () => {
      alive = false;
    };
  }, [id]);

  const tok = d
    ? `in ${d.input_tokens} · out ${d.output_tokens}` +
      (d.cache_read_tokens ? ` · cached ${d.cache_read_tokens}` : "")
    : "";

  return (
    <ModalShell
      icon="🧠"
      cls="ev-tool"
      title={`LLM call · ${d?.kind ?? ""}`}
      chip="llm"
      time={d ? fullTime(d.created_at) : ""}
      onClose={onClose}
    >
      {missing && <p className="brain-empty">This call's I/O wasn't recorded.</p>}
      {!d && !missing && <p className="brain-empty">Loading…</p>}
      {d && (
        <>
          <p className="io-meta">
            {d.model} · {tok}
          </p>
          <div className="section">
            <h3>System prompt</h3>
            {d.system_prompt ? (
              <pre className="io-pre">{d.system_prompt}</pre>
            ) : (
              <p className="brain-empty small">none</p>
            )}
          </div>
          <div className="section">
            <h3>Request</h3>
            <RequestView raw={d.request} />
          </div>
          <div className="section">
            <h3>Reply</h3>
            {d.response ? (
              <pre className="io-pre">{d.response}</pre>
            ) : (
              <p className="brain-empty small">empty</p>
            )}
          </div>
          <div className="section">
            <h3>Tool calls</h3>
            <ToolCallsView raw={d.tool_calls} />
          </div>
        </>
      )}
    </ModalShell>
  );
}
