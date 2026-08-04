import { useState } from "react";
import type { MediaItem } from "../types.ts";
import { WorldFilterBar, type WorldFilter } from "./WorldFilterBar.tsx";
import { usePolledFetch } from "../usePolledFetch.ts";
import { fullTime } from "./ModalShell.tsx";

const POLL_MS = 10000;

// Show Moko's own pictures by default; received files and other kinds are a toggle away.
type Filter = "made" | "received" | "all";
const FILTERS: { key: Filter; label: string }[] = [
  { key: "made", label: "Moko made" },
  { key: "received", label: "Received" },
  { key: "all", label: "All" },
];

export function MediaGallery() {
  const [filter, setFilter] = useState<Filter>("made");
  const [world, setWorld] = useState<WorldFilter>("all");
  const [zoom, setZoom] = useState<MediaItem | null>(null);

  // World is filtered server-side (?world=…) so it isn't truncated by the fetch window;
  // direction stays a cheap client-side toggle over what's loaded.
  const url = `/api/media?limit=120${world === "all" ? "" : `&world=${world}`}`;
  const { data, reload } = usePolledFetch<MediaItem[]>(url, POLL_MS);
  const items = data ?? [];
  const shown = items.filter((m) =>
    filter === "all" ? true : filter === "made" ? m.direction === "out" : m.direction === "in",
  );

  return (
    <section className="brain media">
      <div className="brain-tabs">
        {FILTERS.map((f) => (
          <button
            key={f.key}
            className={`btab ${filter === f.key ? "on" : ""}`}
            onClick={() => setFilter(f.key)}
          >
            {f.label}
          </button>
        ))}
        <button className="refresh" title="Refresh" onClick={reload}>
          ↻
        </button>
      </div>

      <WorldFilterBar world={world} onChange={setWorld} />

      <div className="brain-body">
        {shown.length === 0 ? (
          <p className="brain-empty">No pictures here yet. 🌸</p>
        ) : (
          <div className="media-grid">
            {shown.map((m) => (
              <MediaCard key={m.id} item={m} onZoom={() => setZoom(m)} />
            ))}
          </div>
        )}
      </div>

      {zoom && <Lightbox item={zoom} onClose={() => setZoom(null)} />}
    </section>
  );
}

function MediaCard({ item, onZoom }: { item: MediaItem; onZoom: () => void }) {
  const src = `/api/media/blob/${item.id}`;
  const label = item.caption || item.description || item.filename || item.kind;
  return (
    <figure className="media-card">
      {item.kind === "image" ? (
        // Grid shows a small cached thumbnail; the lightbox loads the full image.
        <img src={`${src}?thumb=1`} alt={label ?? ""} loading="lazy" onClick={onZoom} />
      ) : item.kind === "audio" ? (
        <audio controls src={src} preload="none" />
      ) : item.kind === "video" ? (
        <video controls src={src} preload="none" />
      ) : (
        <a className="media-doc" href={src} target="_blank" rel="noreferrer">
          📄 {item.filename || "document"}
        </a>
      )}
      <figcaption>
        <span className={`media-dir ${item.direction}`}>
          {item.direction === "out" ? "Moko" : "received"}
        </span>
        <span className="media-label" title={label ?? ""}>
          {label}
        </span>
        <span className="media-when">{fullTime(item.created_at)}</span>
      </figcaption>
    </figure>
  );
}

function Lightbox({ item, onClose }: { item: MediaItem; onClose: () => void }) {
  return (
    <div className="lightbox" onClick={onClose}>
      <img src={`/api/media/blob/${item.id}`} alt={item.caption ?? ""} />
    </div>
  );
}
