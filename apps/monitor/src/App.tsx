import { useEffect, useRef, useState } from "react";
import type { MokoEvent, Status } from "./types.ts";
import { TopBar } from "./components/TopBar.tsx";
import { Rail } from "./components/Rail.tsx";
import { Feed } from "./components/Feed.tsx";
import { BrainBrowser } from "./components/BrainBrowser.tsx";
import { DreamsView } from "./components/DreamsView.tsx";
import { UsageView } from "./components/UsageView.tsx";
import { MediaGallery } from "./components/MediaGallery.tsx";
import { usePolledFetch } from "./usePolledFetch.ts";

type View = "live" | "brain" | "dreams" | "usage" | "media";

const MAX_EVENTS = 600; // keep the feed bounded; the bus backfills recent on connect

export default function App() {
  const { data: status } = usePolledFetch<Status>("/api/status", 5000);
  const [events, setEvents] = useState<MokoEvent[]>([]);
  const [connected, setConnected] = useState(false);
  const [view, setView] = useState<View>("live");
  const seq = useRef(0); // stable client-side keys (events carry no id of their own)

  // Live feed: subscribe to the SSE stream. EventSource auto-reconnects.
  useEffect(() => {
    const es = new EventSource("/api/stream");
    es.onopen = () => setConnected(true);
    es.onerror = () => setConnected(false);
    es.onmessage = (msg) => {
      try {
        const event = JSON.parse(msg.data) as MokoEvent;
        (event as { _k?: number })._k = seq.current++;
        setEvents((prev) => {
          const next = [event, ...prev]; // newest first
          return next.length > MAX_EVENTS ? next.slice(0, MAX_EVENTS) : next;
        });
      } catch {
        /* ignore a malformed frame */
      }
    };
    return () => es.close();
  }, []);

  const tabs: { key: View; label: string }[] = [
    { key: "live", label: "Live feed" },
    { key: "brain", label: "Brain" },
    { key: "dreams", label: "Dreams" },
    { key: "usage", label: "Usage" },
    { key: "media", label: "Media" },
  ];

  return (
    <div className="app">
      <TopBar status={status} connected={connected} />
      <nav className="nav">
        {tabs.map((t) => (
          <button key={t.key} className={view === t.key ? "on" : ""} onClick={() => setView(t.key)}>
            {t.label}
          </button>
        ))}
      </nav>
      <div className="body">
        <main className="main">
          {view === "live" && <Feed events={events} onClear={() => setEvents([])} />}
          {view === "brain" && <BrainBrowser counts={status?.counts} />}
          {view === "dreams" && <DreamsView />}
          {view === "usage" && <UsageView />}
          {view === "media" && <MediaGallery />}
        </main>
        <Rail status={status} />
      </div>
    </div>
  );
}
