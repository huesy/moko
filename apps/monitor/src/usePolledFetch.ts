import { useCallback, useEffect, useState } from "react";

// Poll a JSON endpoint on an interval. One home for the fetch / try-catch / setInterval /
// unmount-guard every view used to re-implement (two of them without the guard). Returns the
// latest data, a transient-error flag, and a manual reload (for a refresh button). Changing
// `url` re-fetches immediately.
export function usePolledFetch<T>(
  url: string,
  ms: number,
): { data: T | null; error: boolean; reload: () => void } {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState(false);
  const [nonce, setNonce] = useState(0);

  useEffect(() => {
    let alive = true;
    const load = async () => {
      try {
        const r = await fetch(url);
        if (alive && r.ok) {
          setData((await r.json()) as T);
          setError(false);
        }
      } catch {
        if (alive) setError(true);
      }
    };
    load();
    const t = setInterval(load, ms);
    return () => {
      alive = false;
      clearInterval(t);
    };
  }, [url, ms, nonce]);

  // A url change is a NEW query (e.g. the gallery's world switch) — blank stale data while the
  // new fetch is in flight, so a consumer never renders the previous query's results (nor keeps
  // them on a failed fetch). A manual reload bumps `nonce`, not `url`, so it never flashes; a
  // static-url consumer only fires this once at mount, where data is already null.
  useEffect(() => setData(null), [url]);

  const reload = useCallback(() => setNonce((n) => n + 1), []);
  return { data, error, reload };
}
