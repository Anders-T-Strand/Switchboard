import { createContext, useCallback, useContext, useEffect, useRef, useState } from "react";
import type { ConfigView, Status } from "./api";
import type { ColorMap } from "./colors";

type Loadable<T> = { data: T | null; error: string | null; loading: boolean; reload: () => void };

/** Fetch on mount and whenever deps change. Optional polling interval in ms. */
export function useApi<T>(fetcher: () => Promise<T>, deps: unknown[], pollMs?: number): Loadable<T> {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [tick, setTick] = useState(0);
  const fetcherRef = useRef(fetcher);
  fetcherRef.current = fetcher;

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    fetcherRef
      .current()
      .then((d) => {
        if (!cancelled) {
          setData(d);
          setError(null);
        }
      })
      .catch((e: Error) => !cancelled && setError(e.message))
      .finally(() => !cancelled && setLoading(false));
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [...deps, tick]);

  useEffect(() => {
    if (!pollMs) return;
    const id = window.setInterval(() => setTick((t) => t + 1), pollMs);
    return () => window.clearInterval(id);
  }, [pollMs]);

  const reload = useCallback(() => setTick((t) => t + 1), []);
  return { data, error, loading, reload };
}

export function useHashRoute(defaultRoute: string): [string, (r: string) => void] {
  const read = () => window.location.hash.replace(/^#\/?/, "").split("?")[0] || defaultRoute;
  const [route, setRoute] = useState(read);
  useEffect(() => {
    const onHash = () => setRoute(read());
    window.addEventListener("hashchange", onHash);
    return () => window.removeEventListener("hashchange", onHash);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);
  const go = useCallback((r: string) => {
    window.location.hash = `/${r}`;
  }, []);
  return [route, go];
}

export function hashParam(name: string): string | null {
  const q = window.location.hash.split("?")[1];
  return q ? new URLSearchParams(q).get(name) : null;
}

export type AppContextValue = {
  config: ConfigView | null;
  status: Status | null;
  colors: ColorMap;
  label: (key: string | null | undefined) => string;
  modelKeys: string[];
};

export const AppContext = createContext<AppContextValue>({
  config: null,
  status: null,
  colors: {},
  label: (k) => k ?? "–",
  modelKeys: [],
});

export const useApp = () => useContext(AppContext);
