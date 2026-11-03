import { useCallback, useEffect, useMemo, useState } from "react";
import { loadConfig, type Config } from "./config";
import { ApiSource, apiReachable, loadSession, logout, saveSession, SESSION_ENDED, type Session } from "./data/api";
import { SnapshotSource } from "./data/snapshot";
import type { DataSource } from "./data/source";
import { when } from "./lib/format";
import { refreshAll, useResource } from "./lib/useResource";
import { ChangePassword } from "./views/Account";
import { Activity } from "./views/Activity";
import { Connect } from "./views/Connect";
import type { Ctx, View } from "./views/context";
import { Conversation } from "./views/Conversation";
import { Customers } from "./views/Customers";
import { Inbox } from "./views/Inbox";
import { Orders } from "./views/Orders";
import { Overview } from "./views/Overview";
import { Results } from "./views/Results";
import { Spend } from "./views/Spend";
import { Worker } from "./views/Worker";

type Boot =
  | { state: "loading" }
  | { state: "connect"; config: Config; reason?: string }
  | { state: "ready"; config: Config; source: DataSource; banner?: string }
  | { state: "error"; message: string };

const VIEWS: { id: View; label: string; title: string; scoped: boolean; devOnly?: boolean }[] = [
  { id: "overview", label: "Overview", title: "All shops", scoped: false },
  { id: "orders", label: "Work orders", title: "Work orders", scoped: true },
  { id: "inbox", label: "Inbox", title: "Inbox", scoped: true },
  { id: "customers", label: "Customers", title: "Customers", scoped: true },
  { id: "activity", label: "Activity", title: "Activity", scoped: true },
  { id: "spend", label: "Spend & model calls", title: "Spend & model calls", scoped: true },
  { id: "results", label: "Eval results", title: "The 40 sample messages", scoped: false },
  // Shown only when the API runs with DEV_MODE=true (local `make dev` / `make dev-step`).
  { id: "worker", label: "Worker & logs (dev)", title: "Worker, job queue and logs", scoped: false, devOnly: true },
];

const viewFromHash = (): View => {
  const h = window.location.hash.replace("#", "");
  return (VIEWS.find((v) => v.id === h)?.id ?? "overview") as View;
};

function remember(key: string, value?: string): string | null {
  try {
    if (value !== undefined) localStorage.setItem(key, value);
    return localStorage.getItem(key);
  } catch {
    return null;
  }
}

export function App() {
  const [boot, setBoot] = useState<Boot>({ state: "loading" });

  const showSnapshot = useCallback(async (config: Config, banner?: string) => {
    try {
      setBoot({ state: "ready", config, source: await SnapshotSource.load(config.snapshotUrl), banner });
    } catch (e) {
      setBoot({ state: "error", message: e instanceof Error ? e.message : String(e) });
    }
  }, []);

  const start = useCallback(async () => {
    const config = await loadConfig();
    if (config.mode === "snapshot") return showSnapshot(config);
    if (!(await apiReachable(config.apiBase))) {
      if (config.mode === "auto") return showSnapshot(config, "The API isn't reachable, so this is the bundled demo snapshot.");
      return setBoot({ state: "connect", config, reason: "The API isn't answering right now." });
    }
    const session = loadSession();
    if (session && session.apiBase === config.apiBase && session.shops.length) {
      return setBoot({ state: "ready", config, source: new ApiSource(session, config.snapshotUrl) });
    }
    setBoot({ state: "connect", config });
  }, [showSnapshot]);

  useEffect(() => {
    void start();
  }, [start]);

  // The API stopped accepting this session (expired, signed out elsewhere, account disabled).
  const config = boot.state === "ready" ? boot.config : null;
  useEffect(() => {
    if (!config) return;
    const ended = () => {
      saveSession(null);
      setBoot({ state: "connect", config, reason: "Your session has ended. Please sign in again." });
    };
    window.addEventListener(SESSION_ENDED, ended);
    return () => window.removeEventListener(SESSION_ENDED, ended);
  }, [config]);

  if (boot.state === "loading") return <div className="connect"><div className="skeleton" style={{ width: 320 }} /></div>;
  if (boot.state === "error") return <div className="connect"><div className="banner bad">{boot.message}</div></div>;
  if (boot.state === "connect") {
    return (
      <Connect
        apiBase={boot.config.apiBase}
        reason={boot.reason}
        onConnected={(s: Session) => {
          saveSession(s);
          setBoot({ state: "ready", config: boot.config, source: new ApiSource(s, boot.config.snapshotUrl) });
        }}
        onUseSnapshot={() => void showSnapshot(boot.config)}
      />
    );
  }
  return (
    <Console
      source={boot.source}
      banner={boot.banner}
      onSignOut={() => {
        if (boot.source instanceof ApiSource) void logout(boot.source.session);
        saveSession(null);
        setBoot({ state: "connect", config: boot.config });
      }}
    />
  );
}

function Console({ source, banner, onSignOut }: { source: DataSource; banner?: string; onSignOut: () => void }) {
  const shops = source.shops();
  const [view, setView] = useState<View>(viewFromHash);
  const [shopId, setShopId] = useState(() => {
    const saved = remember("riverbend.shop");
    return shops.find((s) => s.id === saved)?.id ?? shops[0]?.id ?? "";
  });
  const [open, setOpen] = useState<{ shop: string; id: string } | null>(null);
  const [customer, setCustomer] = useState<{ shop: string; id: string } | null>(null);
  const [toast, setToast] = useState<string | null>(null);
  const [changingPassword, setChangingPassword] = useState(false);
  const session = source instanceof ApiSource ? source.session : null;

  useEffect(() => {
    const onHash = () => setView(viewFromHash());
    window.addEventListener("hashchange", onHash);
    return () => window.removeEventListener("hashchange", onHash);
  }, []);
  useEffect(() => {
    if (!toast) return;
    const t = window.setTimeout(() => setToast(null), 2600);
    return () => window.clearTimeout(t);
  }, [toast]);

  const names = useMemo(() => new Map(shops.map((s) => [s.id, s.name])), [shops]);
  const ctx: Ctx = {
    source,
    shopId,
    shopName: (id) => names.get(id) ?? id,
    // Auto-refresh is switched off for now: staff press Refresh in the top bar instead.
    // To switch it back on, uncomment the next line (useResource pauses it while the tab is hidden).
    // poll: source.kind === "live" ? 15_000 : undefined,
    openConversation: (shop, id) => setOpen({ shop, id }),
    openCustomer: (shop, id) => {
      setOpen(null);
      setCustomer({ shop, id });
      ctx.go("customers", shop);
    },
    go: (v, shop) => {
      if (shop) {
        setShopId(shop);
        remember("riverbend.shop", shop);
      }
      setView(v);
      try {
        history.replaceState(null, "", `#${v}`);
      } catch {
        /* sandboxed frames may refuse; in-app state still changes */
      }
    },
    toast: setToast,
  };

  const inbox = useResource(() => source.inbox(shopId), `nav-inbox:${shopId}`, ctx.poll);
  const needs = inbox.data?.items.filter((i) => i.needs_action).length ?? 0;
  const dev = useResource(() => source.devTools(), "nav-dev");
  const views = VIEWS.filter((v) => !v.devOnly || dev.data);
  const meta = VIEWS.find((v) => v.id === view)!;
  const snapshot = source instanceof SnapshotSource ? source.snapshot : null;

  return (
    <div className="app">
      <nav className="rail" aria-label="Views">
        <div className="brand">
          <span>Riverbend Cycles</span>
          <b>Work orders</b>
        </div>
        <div className="nav">
          {views.map((v) => (
            <button key={v.id} aria-current={view === v.id ? "page" : undefined} onClick={() => ctx.go(v.id)}>
              {v.label}
              {v.id === "inbox" && needs ? <span className="count">{needs}</span> : null}
            </button>
          ))}
        </div>
        <div className="rail-foot">
          {source.kind === "live" ? (
            <>
              {session?.account ? (
                <span className="who" title={session.account.email}>
                  Signed in as <b>{session.account.name || session.account.email}</b>
                </span>
              ) : null}
              <span className="session-note">Live API · {shops.length} shop{shops.length === 1 ? "" : "s"} in this session</span>
              {session?.account ? (
                <button className="btn" onClick={() => setChangingPassword(true)}>Change password</button>
              ) : null}
              <button className="btn" onClick={onSignOut}>Sign out</button>
            </>
          ) : (
            <span>Snapshot · {snapshot ? when(snapshot.generatedAt) : ""} · read-only</span>
          )}
        </div>
      </nav>

      <div className="main">
        <div className="topbar">
          <h1>{meta.scoped ? `${meta.title} · ${ctx.shopName(shopId).replace("Riverbend Cycles ", "")}` : meta.title}</h1>
          <span className="spacer" />
          {meta.scoped && shops.length > 1 ? (
            <div className="seg" role="group" aria-label="Shop">
              {shops.map((s) => (
                <button key={s.id} aria-pressed={s.id === shopId} onClick={() => ctx.go(view, s.id)}>
                  {s.name.replace("Riverbend Cycles ", "")}
                </button>
              ))}
            </div>
          ) : null}
          {source.kind === "live" ? <RefreshButton onDone={() => setToast("Refreshed")} /> : null}
          <span className={`chip ${source.kind === "live" ? "ok" : "warn"}`}>
            {source.kind === "live" ? "Live" : "Snapshot"}
          </span>
        </div>

        <div className="content">
          {banner ? <div className="banner">{banner}</div> : null}
          {snapshot && view !== "results" ? (
            <div className="banner info">
              <b>Read-only snapshot.</b> {snapshot.note}
            </div>
          ) : null}
          {view === "overview" && <Overview ctx={ctx} />}
          {view === "orders" && <Orders ctx={ctx} />}
          {view === "inbox" && <Inbox ctx={ctx} />}
          {view === "customers" && (
            <Customers ctx={ctx} selected={customer?.shop === shopId ? customer.id : null}
                       onSelect={(id) => setCustomer(id ? { shop: shopId, id } : null)} />
          )}
          {view === "activity" && <Activity ctx={ctx} />}
          {view === "spend" && <Spend ctx={ctx} />}
          {view === "results" && <Results ctx={ctx} />}
          {view === "worker" && <Worker ctx={ctx} />}
        </div>
      </div>

      {open ? <Conversation ctx={ctx} shopId={open.shop} conversationId={open.id} onClose={() => setOpen(null)} /> : null}
      {changingPassword && session ? (
        <ChangePassword
          session={session}
          onClose={() => setChangingPassword(false)}
          onDone={() => {
            setChangingPassword(false);
            setToast("Password changed. Other sessions were signed out.");
          }}
        />
      ) : null}
      {toast ? <div className="toast" role="status">{toast}</div> : null}
    </div>
  );
}

/** Reloads everything on screen: the open view, the conversation drawer and the inbox count. */
function RefreshButton({ onDone }: { onDone: () => void }) {
  const [busy, setBusy] = useState(false);
  return (
    <button
      className="btn"
      disabled={busy}
      onClick={() => {
        setBusy(true);
        void refreshAll().finally(() => {
          setBusy(false);
          onDone();
        });
      }}
    >
      {busy ? "Refreshing…" : "Refresh"}
    </button>
  );
}
