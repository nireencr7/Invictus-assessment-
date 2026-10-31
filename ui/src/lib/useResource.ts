import { useCallback, useEffect, useRef, useState } from "react";

interface State<T> {
  data?: T;
  error?: string;
  loading: boolean;
}

/**
 * Load (and optionally poll) one resource. When the key changes — e.g. the shop switches — the
 * previous data is dropped immediately, so one shop's records never flash under another's name.
 */
export function useResource<T>(load: () => Promise<T>, key: string, pollMs?: number) {
  const [state, setState] = useState<State<T>>({ loading: true });
  const loader = useRef(load);
  loader.current = load;
  const [tick, setTick] = useState(0);
  const lastKey = useRef(key);

  useEffect(() => {
    let alive = true;
    if (lastKey.current !== key) {
      lastKey.current = key;
      setState({ loading: true });
    }
    const run = () =>
      loader.current().then(
        (data) => alive && setState({ data, loading: false }),
        (e: unknown) =>
          alive && setState((s) => ({ ...s, loading: false, error: e instanceof Error ? e.message : String(e) })),
      );
    run();
    const timer = pollMs ? window.setInterval(run, pollMs) : undefined;
    return () => {
      alive = false;
      if (timer) window.clearInterval(timer);
    };
  }, [key, pollMs, tick]);

  const reload = useCallback(() => setTick((t) => t + 1), []);
  return { ...state, reload };
}
