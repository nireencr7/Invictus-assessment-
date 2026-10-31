import type { ActivityItem, ConversationDetail, EvalSummary, Inbox, LlmCall, Shop, Spend, WorkOrderList } from "../types";
import { SourceError, type Actions, type DataSource } from "./source";

/** What a signed-in session holds. Lives in sessionStorage, so it ends with the tab. */
export interface Session {
  apiBase: string;
  /** One staff token per shop; each token only ever unlocks its own shop. */
  shops: { id: string; name: string; token: string }[];
  adminToken?: string;
  ingestKey?: string;
}

const KEY = "riverbend.session";

export function loadSession(): Session | null {
  try {
    const raw = sessionStorage.getItem(KEY);
    return raw ? (JSON.parse(raw) as Session) : null;
  } catch {
    return null;
  }
}

export function saveSession(s: Session | null) {
  try {
    if (s) sessionStorage.setItem(KEY, JSON.stringify(s));
    else sessionStorage.removeItem(KEY);
  } catch {
    /* storage blocked: the session just won't survive a reload */
  }
}

async function call<T>(base: string, path: string, init: RequestInit = {}): Promise<T> {
  let r: Response;
  try {
    r = await fetch(base + path, init);
  } catch {
    throw new SourceError("Can't reach the API. Check that it's running and the address is right.");
  }
  const body = await r.json().catch(() => ({}));
  if (!r.ok) {
    const detail = typeof body?.detail === "string" ? body.detail : `HTTP ${r.status}`;
    throw new SourceError(detail, r.status);
  }
  return body as T;
}

/** Sign in with a staff token: the API says which shop it belongs to. */
export async function whoAmI(apiBase: string, token: string): Promise<Shop> {
  const me = await call<{ shop_id: string; shop_name: string }>(apiBase, "/v1/me", {
    headers: { Authorization: `Bearer ${token}` },
  });
  return { id: me.shop_id, name: me.shop_name };
}

/** Local development only: the API publishes its dev credentials when DEV_MODE=true. */
export async function devSession(apiBase: string): Promise<Session> {
  const cfg = await call<{
    shops: Shop[];
    staff_tokens: Record<string, string>;
    ingest_key: string | null;
    admin_token: string;
  }>(apiBase, "/v1/dev/config");
  return {
    apiBase,
    shops: cfg.shops.map((s) => ({ ...s, token: cfg.staff_tokens[s.id] })),
    adminToken: cfg.admin_token,
    ingestKey: cfg.ingest_key ?? undefined,
  };
}

export async function apiReachable(apiBase: string): Promise<boolean> {
  try {
    const r = await fetch(apiBase + "/readyz", { cache: "no-store" });
    return r.ok;
  } catch {
    return false;
  }
}

export class ApiSource implements DataSource {
  readonly kind = "live" as const;
  readonly actions: Actions;

  constructor(
    readonly session: Session,
    private readonly evalUrl: string | null,
  ) {
    const s = session;
    this.actions = {
      retryAi: async (shop, id) => {
        await call(s.apiBase, `/v1/shops/${shop}/conversations/${id}/retry`, { method: "POST", headers: this.staff(shop) });
      },
      setWorkOrderStatus: async (shop, id, status) => {
        await call(s.apiBase, `/v1/shops/${shop}/work-orders/${id}/status`, {
          method: "POST",
          headers: { ...this.staff(shop), "Content-Type": "application/json" },
          body: JSON.stringify({ status }),
        });
      },
      setCap: s.adminToken
        ? async (shop, usd) => {
            const r = await call<{ released_held_messages: number }>(s.apiBase, `/v1/admin/shops/${shop}/budget`, {
              method: "PUT",
              headers: { Authorization: `Bearer ${s.adminToken}`, "Content-Type": "application/json" },
              body: JSON.stringify({ monthly_cap_usd: usd }),
            });
            return { released: r.released_held_messages };
          }
        : undefined,
      sendMessage: s.ingestKey
        ? async (shop, body, channel, replyToken) => {
            const payload: Record<string, string> = {
              message_id: `console-${crypto.randomUUID().slice(0, 8)}`,
              channel,
              received_at: new Date().toISOString(),
              body,
            };
            if (replyToken) payload.reply_token = replyToken;
            return call<{ duplicate: boolean }>(s.apiBase, `/v1/shops/${shop}/inbound-messages`, {
              method: "POST",
              headers: { "X-Ingest-Key": s.ingestKey!, "Content-Type": "application/json" },
              body: JSON.stringify(payload),
            });
          }
        : undefined,
    };
  }

  private staff(shop: string): HeadersInit {
    const entry = this.session.shops.find((s) => s.id === shop);
    if (!entry) throw new SourceError("This session has no access to that shop", 404);
    return { Authorization: `Bearer ${entry.token}` };
  }

  private get<T>(shop: string, path: string): Promise<T> {
    return call<T>(this.session.apiBase, path, { headers: this.staff(shop) });
  }

  shops() {
    return this.session.shops.map(({ id, name }) => ({ id, name }));
  }

  workOrders(shop: string, scope: "open" | "all") {
    return this.get<WorkOrderList>(shop, `/v1/shops/${shop}/work-orders?status=${scope}`);
  }

  inbox(shop: string) {
    return this.get<Inbox>(shop, `/v1/shops/${shop}/inbox`);
  }

  spend(shop: string) {
    return this.get<Spend>(shop, `/v1/shops/${shop}/spend`);
  }

  async llmCalls(shop: string) {
    return (await this.get<{ items: LlmCall[] }>(shop, `/v1/shops/${shop}/llm-calls?limit=200`)).items;
  }

  async activity(shop: string) {
    return (await this.get<{ items: ActivityItem[] }>(shop, `/v1/shops/${shop}/activity?limit=150`)).items;
  }

  conversation(shop: string, id: string) {
    return this.get<ConversationDetail>(shop, `/v1/shops/${shop}/conversations/${id}`);
  }

  /** Eval results are a build artefact, not live data: read them from the bundled snapshot. */
  async evalSummary(): Promise<EvalSummary | null> {
    if (!this.evalUrl) return null;
    try {
      const r = await fetch(this.evalUrl, { cache: "no-store" });
      return r.ok ? ((await r.json()).eval as EvalSummary) : null;
    } catch {
      return null;
    }
  }
}
