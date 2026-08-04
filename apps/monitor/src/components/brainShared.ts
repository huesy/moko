import type { GapDetail, SubjectDetail, ThoughtDetail } from "../types.ts";

// Shared across the brain browser's container, its card renderers, and its detail modal —
// the one home for the tab set, the navigable-record types, and the valence formatter so the
// three files can't drift.

export const TABS = [
  "thoughts",
  "subjects",
  "gaps",
  "commitments",
  "outreach",
  "messages",
] as const;
export type Tab = (typeof TABS)[number];

// The three record kinds that open a navigable detail card (vs the static RecordModal).
// They cross-link to each other: subject ↔ thought ↔ gap.
export type DetailKind = "subject" | "thought" | "gap";
export type Detail =
  | { kind: "subject"; data: SubjectDetail }
  | { kind: "thought"; data: ThoughtDetail }
  | { kind: "gap"; data: GapDetail };

// The world axis only applies to world-bound tabs; elsewhere it's a true no-op (so changing
// it on messages neither re-fetches nor resets the list).
export const WORLD_BOUND = new Set<Tab>(["subjects", "thoughts", "gaps"]);

export function valence(v: number | null | undefined): string {
  if (v == null) return "";
  if (v > 0.15) return `🙂 +${v.toFixed(2)}`;
  if (v < -0.15) return `🙁 ${v.toFixed(2)}`;
  return `· ${v.toFixed(2)}`;
}
