/**
 * Runtime configuration, read from config.json next to index.html. The container writes that
 * file from environment variables at start-up, so one image runs in every environment.
 *   mode: "live"      always use the API
 *         "snapshot"  never touch an API; show the bundled demo dataset (read-only)
 *         "auto"      use the API if it answers, else fall back to the snapshot
 */
export interface Config {
  mode: "live" | "snapshot" | "auto";
  apiBase: string;
  snapshotUrl: string;
}

const DEFAULTS: Config = { mode: "auto", apiBase: "", snapshotUrl: "demo-snapshot.json" };

export async function loadConfig(): Promise<Config> {
  try {
    const r = await fetch("config.json", { cache: "no-store" });
    if (!r.ok) return DEFAULTS;
    const c = (await r.json()) as Partial<Config>;
    return {
      mode: c.mode === "live" || c.mode === "snapshot" ? c.mode : "auto",
      apiBase: (c.apiBase ?? "").replace(/\/$/, ""),
      snapshotUrl: c.snapshotUrl || DEFAULTS.snapshotUrl,
    };
  } catch {
    return DEFAULTS;
  }
}
