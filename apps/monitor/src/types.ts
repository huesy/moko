// Events are intentionally loose — the bus is an open contract and new event
// types can appear without a frontend change. We render whatever arrives,
// styling the types we know and falling back gracefully for the rest.
export interface MokoEvent {
  type: string;
  at?: string;
  id?: string; // some events carry a row id (e.g. llm.call / media → deep-link)
  preview?: string; // llm.call: a short snippet of the reply
  media_kind?: string; // media.* events: image | audio | video | document
  gen_prompt?: string; // media.generated: the exact generation prompt
  filename?: string; // media.* events
  name?: string; // snapshot: backup name / learned: subject name
  kb?: number; // snapshot: size
  what?: string; // learned: subject | fact | link ; failure: media
  key?: string; // learned (fact): attribute key
  value?: string; // learned (fact): attribute value
  rel_type?: string; // learned (link): relationship type
  due_at?: string | null; // commitment.created
  // Common, type-dependent fields:
  channel?: string;
  direction?: "in" | "out";
  body?: string;
  text?: string;
  tool?: string;
  args?: unknown;
  result?: string;
  kind?: string;
  shareability?: string;
  valence?: number | null;
  recorded?: boolean;
  input_tokens?: number | null;
  output_tokens?: number | null;
  cache_read_tokens?: number | null;
  verdict?: string;
  action?: string;
  gap?: string;
  sentiment?: string | null;
  mode?: string; // reflection: "fresh" | "muse"
  act?: boolean; // outreach_consult: reached out, or chose to wait
  reason?: string; // outreach_consult: the brain's private reasoning
  reconsider_in_minutes?: number | null; // outreach_consult: when it'll revisit
  prompt?: { system?: string; user?: string }; // outreach_consult: the exact LLM input behind the decision (ephemeral, never persisted)
  description?: string; // gap.opened / gap.resolved: the curiosity's text
  resolution_path?: string; // gap.opened: ask | research | either
  category?: string; // gate: the tool's sensitivity category
  stats?: Record<string, number> | null; // dream.end: counts of what was consolidated
  [key: string]: unknown;
}

// ---- Autonomous rhythms (countdown bar) ----

export interface Rhythm {
  name: string; // settle | reflection | sleeping | outreach | research
  state: string; // cooling | eligible | quiet_hours | resting | scheduled | queued | idle | …
  next_at: string | null; // ISO UTC the floor next lifts; client counts down to it
  detail: string;
}

// ---- Brain browser (read-only views of the mind) ----

export interface Thought {
  id: string;
  kind: string;
  body: string;
  valence: number | null;
  shareability: string;
  prudence: number;
  stability: number;
  created_at: string;
  world_id?: string | null; // M11
}

export interface Gap {
  id: string;
  about_id: string | null;
  description: string;
  kind: string;
  resolution_path: string;
  salience: number;
  status: string;
  opened_at: string;
  world_id?: string | null; // M11
}

export interface Commitment {
  id: string;
  description: string;
  kind: string;
  about_id: string | null;
  due_at: string | null;
  status: string;
  created_at: string;
}

export interface BrainMessage {
  id: string;
  channel: string;
  direction: "in" | "out";
  body: string;
  sentiment: string | null;
  sent_at: string;
}

export interface Subject {
  id: string;
  kind: string;
  name: string;
  confidence: number;
  notes: string;
  created_at: string;
  world_id?: string | null; // M11: 'world-companion' | 'world-moko'
}

export interface Attribute {
  id: string;
  key: string;
  value: string;
  value_type: string;
  valid_from: string | null;
  valid_to: string | null;
  confidence: number;
}

export interface Relationship {
  id: string;
  type: string;
  direction: "in" | "out";
  other_id: string;
  other_name: string;
  valid_from: string | null;
  valid_to: string | null;
}

// A compact, clickable reference to a subject — enough to label and navigate.
export interface SubjectRef {
  id: string;
  name: string;
  kind: string;
}

export interface SubjectDetail {
  subject: Subject;
  attributes: Attribute[];
  relationships: Relationship[];
  thoughts: Thought[];
  gaps: Gap[]; // the curiosities about this subject
}

// A thought with the subjects it's about and the gaps about those same subjects,
// so a thought and a same-subject curiosity are navigable to each other.
export interface ThoughtDetail {
  thought: Thought;
  subjects: SubjectRef[];
  gaps: Gap[];
}

// The inverse: a gap with its subject subject and Moko's thoughts about that subject.
export interface GapDetail {
  gap: Gap;
  subject: SubjectRef | null;
  thoughts: Thought[];
}

export interface Dream {
  id: string;
  started_at: string;
  ended_at: string | null;
  summary: string;
  wonders: string; // Moko's private one-line reflection after consolidating
  stats: Record<string, number>; // counts of what each consolidation step did
  duration_seconds: number | null;
}

export interface Status {
  moko_version: string;
  python: string;
  companion: string;
  timezone: string;
  now: string;
  llm: { provider: string; model: string; auth: string };
  observers: number;
  counts?: Record<string, number>;
  service?: { available?: boolean; active?: boolean; [k: string]: unknown };
  subscription?: { signed_in: boolean; hours_left?: number };
}

// ── Token Tracker (Usage tab) ──────────────────────────────────────────────
export interface UsageKindRow {
  kind: string;
  calls: number;
  input_tokens: number;
  output_tokens: number;
  cache_read_tokens: number;
  cache_write_tokens: number;
}

export interface UsageSummary {
  by_kind: UsageKindRow[];
  totals: Omit<UsageKindRow, "kind">;
}

export interface UsageRow {
  id: string;
  created_at: string;
  kind: string;
  model: string | null;
  input_tokens: number;
  output_tokens: number;
  cache_read_tokens: number;
  cache_write_tokens: number;
}

export interface Usage {
  rows: UsageRow[];
  all_time: UsageSummary;
  window_24h: UsageSummary;
  window_7d: UsageSummary;
  media: Record<string, number>;
}

// ── Media gallery (Media tab) ──────────────────────────────────────────────
export interface MediaItem {
  id: string;
  kind: string; // image | audio | video | document
  mime: string | null;
  direction: string; // in (received) | out (Moko made/sent)
  caption: string | null;
  description: string | null;
  filename: string | null;
  created_at: string;
  world_id?: string | null; // M11: 'world-companion' | 'world-moko'
}
