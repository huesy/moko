// A per-view world filter (M11), shown UNDER the tabs in the world-aware views (Brain's
// subjects/thoughts/gaps, and Media). The world axis only belongs where the data is
// world-tagged, so it lives with those views rather than as a global app control.
export type WorldFilter = "all" | "companion" | "moko";

const OPTS: { key: WorldFilter; label: string }[] = [
  { key: "all", label: "All" },
  { key: "companion", label: "Companion's" },
  { key: "moko", label: "Moko's" },
];

export function WorldFilterBar({
  world,
  onChange,
}: {
  world: WorldFilter;
  onChange: (w: WorldFilter) => void;
}) {
  return (
    <div className="world-bar" title="Whose world to show">
      <span className="world-bar-label">World</span>
      {OPTS.map((o) => (
        <button
          key={o.key}
          className={`wfbtn ${world === o.key ? "on" : ""}`}
          onClick={() => onChange(o.key)}
        >
          {o.label}
        </button>
      ))}
    </div>
  );
}
