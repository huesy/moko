import { useCallback, useEffect, useRef, useState } from "react";
import { RecordModal } from "./RecordModal.tsx";
import type { BrainRecord, RecordKind } from "./RecordModal.tsx";
import { WorldFilterBar, type WorldFilter } from "./WorldFilterBar.tsx";
import { BrainList } from "./brainCards.tsx";
import { DetailModal } from "./brainDetail.tsx";
import { type Detail, type DetailKind, type Tab, TABS, WORLD_BOUND } from "./brainShared.ts";

// One network page. Matches the server's default; a short page keeps first
// paint cheap, and we fetch more as the companion scrolls.
const PAGE = 50;
const NEAR_BOTTOM_PX = 240; // how close to the end before we prefetch the next page

export function BrainBrowser({ counts }: { counts?: Record<string, number> }) {
  const [tab, setTab] = useState<Tab>("thoughts");
  const [world, setWorld] = useState<WorldFilter>("all");
  const [items, setItems] = useState<unknown[]>([]);
  const [loading, setLoading] = useState(false); // first page of a tab
  const [loadingMore, setLoadingMore] = useState(false);
  const [hasMore, setHasMore] = useState(false);
  // A navigation trail of open detail records: the last is shown in the modal,
  // earlier ones are where "back" returns to. Empty = no detail modal open.
  const [trail, setTrail] = useState<Detail[]>([]);
  const current = trail[trail.length - 1] ?? null;
  const [record, setRecord] = useState<{ kind: RecordKind; data: BrainRecord } | null>(null);
  const bodyRef = useRef<HTMLDivElement>(null);
  const reqSeq = useRef(0); // latest-wins guard: ignore a response a newer request superseded

  // The world axis only applies to world-bound tabs; elsewhere it's a true no-op (so changing
  // it on messages neither re-fetches nor resets the list).
  const effectiveWorld: WorldFilter = WORLD_BOUND.has(tab) ? world : "all";

  // Fetch one page at `offset`. The first page replaces; later pages append.
  const fetchPage = useCallback(async (t: Tab, offset: number) => {
    const first = offset === 0;
    const myReq = ++reqSeq.current;
    first ? setLoading(true) : setLoadingMore(true);
    if (first) setTrail([]);
    try {
      const w = WORLD_BOUND.has(t) ? effectiveWorld : "all";
      let url = `/api/brain/${t}?limit=${PAGE}&offset=${offset}`;
      if (w !== "all") url += `&world=${w}`; // M11 world filter (world-bound tabs only)
      const r = await fetch(url);
      const batch = r.ok ? ((await r.json()) as unknown[]) : [];
      if (myReq !== reqSeq.current) return; // a newer tab/world request won — drop this result
      setItems((prev) => (first ? batch : [...prev, ...batch]));
      setHasMore(batch.length === PAGE);
    } catch {
      if (myReq !== reqSeq.current) return;
      if (first) setItems([]);
      setHasMore(false);
    } finally {
      if (myReq === reqSeq.current) first ? setLoading(false) : setLoadingMore(false);
    }
  }, [effectiveWorld]);

  // (Re)load the first page when the tab changes, or the effective world changes (the latter
  // only fires on world-bound tabs, since effectiveWorld is pinned to 'all' elsewhere).
  useEffect(() => {
    fetchPage(tab, 0);
  }, [tab, fetchPage]);

  // Infinite scroll: when the list nears the bottom, pull the next page. Skipped
  // while a detail modal is open (the list is behind it; don't fetch underneath).
  const onScroll = useCallback(() => {
    const el = bodyRef.current;
    if (!el || trail.length || loading || loadingMore || !hasMore) return;
    if (el.scrollTop + el.clientHeight >= el.scrollHeight - NEAR_BOTTOM_PX) {
      fetchPage(tab, items.length);
    }
  }, [trail.length, loading, loadingMore, hasMore, tab, items.length, fetchPage]);

  // Open a navigable record — subject, thought, or gap — into the detail modal,
  // pushing onto the trail so cross-links drill in and "back" steps out again.
  const openDetail = useCallback(async (kind: DetailKind, id: string) => {
    try {
      const r = await fetch(`/api/brain/${kind}/${id}`);
      if (!r.ok) return;
      const data = await r.json();
      setTrail((t) => [...t, { kind, data } as Detail]);
    } catch {
      /* ignore */
    }
  }, []);

  const back = useCallback(() => setTrail((t) => t.slice(0, -1)), []);
  const closeDetail = useCallback(() => setTrail([]), []);

  const openRecord = (kind: RecordKind, data: BrainRecord) => setRecord({ kind, data });

  return (
    <section className="brain">
      <div className="brain-tabs">
        {TABS.map((t) => (
          <button key={t} className={`btab ${tab === t ? "on" : ""}`} onClick={() => setTab(t)}>
            {t}
            {counts?.[t] != null && <span className="btab-count">{counts[t]}</span>}
          </button>
        ))}
        <button className="refresh" title="Refresh" onClick={() => fetchPage(tab, 0)}>
          ↻
        </button>
      </div>

      {/* The world filter lives under the tabs, only where the data is world-tagged. */}
      {WORLD_BOUND.has(tab) && <WorldFilterBar world={world} onChange={setWorld} />}

      <div className="brain-body" ref={bodyRef} onScroll={onScroll}>
        {loading && <p className="brain-empty">Loading…</p>}
        {!loading && items.length === 0 && <p className="brain-empty">Nothing here yet. 🌸</p>}
        {!loading && items.length > 0 && (
          <>
            <BrainList
              tab={tab}
              items={items}
              onOpenDetail={openDetail}
              onOpenRecord={openRecord}
            />
            {loadingMore && <p className="brain-more">Loading more…</p>}
            {!hasMore && <p className="brain-end">· end ·</p>}
          </>
        )}
      </div>

      {current && (
        <DetailModal
          detail={current}
          depth={trail.length}
          onOpen={openDetail}
          onBack={back}
          onClose={closeDetail}
        />
      )}
      {record && (
        <RecordModal kind={record.kind} record={record.data} onClose={() => setRecord(null)} />
      )}
    </section>
  );
}
