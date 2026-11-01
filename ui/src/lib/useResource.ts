import { useCallback, useEffect, useRef, useState } from "react";

interface State<T> {
  data?: T;
  error?: string;
  loading: boolean;
}

// Every resource on screen, so the top bar's Refresh button can reload all of them at once.
const mounted = new Set<() => Promise<void>>();

/** Reload every resource on screen; resolves once they have all answered (or failed). */
export function refreshAll(): Promise<void> {
  return Promise.allSettled([...mounted].map((run) => run())).then(() => undefined);
}

/**
 * Load (and optionally poll) one resource. When the key changes — e.g. the shop switches — the
 * previous data is dropped immediately, so one shop's records never flash under another's name.
 * Polling skips ticks while the tab is hidden and catches up as soon as it's shown again, and a
 * load never starts while the previous one is still in flight.
 */
export function useResource<T>(load: () => Promise<T>, key: string, pollMs?: number) {
  const [state, setState] = useState<State<T>>({ loading: true });
  const loader = useRef(load);
  loader.current = load;
  const [tick, setTick] = useState(0);
  const lastKey = useRef(key);

  useEffect(() => {
    let alive = true;
    let inFlight: Promise<void> | null = null;
    if (lastKey.current !== key) {
      lastKey.current = key;
      setState({ loading: true });
    }
    const run = (): Promise<void> => {
      inFlight ??= loader
        .current()
        .then(
          (data) => {
            if (alive) setState({ data, loading: false });
          },
          (e: unknown) => {
            if (alive) setState((s) => ({ ...s, loading: false, error: e instanceof Error ? e.message : String(e) }));
          },
        )
        .finally(() => {
          inFlight = null;
        });
      return inFlight;
    };
    void run();
    mounted.add(run);

    const visible = () => document.visibilityState === "visible";
    const onShow = () => {
      if (visible()) void run();
    };
    const timer = pollMs ? window.setInterval(onShow, pollMs) : undefined;
    if (pollMs) document.addEventListener("visibilitychange", onShow);
    return () => {
      alive = false;
      mounted.delete(run);
      if (timer) window.clearInterval(timer);
      if (pollMs) document.removeEventListener("visibilitychange", onShow);
    };
  }, [key, pollMs, tick]);

  const reload = useCallback(() => setTick((t) => t + 1), []);
  return { ...state, reload };
}
