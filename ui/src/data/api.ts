import type {
  ActivityItem,
  ConversationDetail,
  CustomerHistory,
  CustomerList,
  EvalSummary,
  Inbox,
  LlmCall,
  Shop,
  Spend,
  StaffReplyResult,
  TestMessageResult,
  WorkOrderList,
} from "../types";
import { SourceError, type Actions, type DataSource } from "./source";

/** What a signed-in session holds. Lives in sessionStorage, so it ends with the tab. */
export interface Session {
  apiBase: string;
  /** One staff token per shop; each token only ever unlocks its own shop. */
  shops: { id: string; name: string; token: string }[];
  adminToken?: string;
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
    // FastAPI sends a string for errors it raises, and a list of field problems for invalid input.
    const detail =
      typeof body?.detail === "string"
        ? body.detail
        : Array.isArray(body?.detail)
          ? body.detail.map((d: { msg?: string }) => (d.msg ?? "").replace(/^Value error, /, "")).filter(Boolean).join("; ")
          : "";
    throw new SourceError(detail || `HTTP ${r.status}`, r.status);
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
    admin_token: string;
  }>(apiBase, "/v1/dev/config");
  return {
    apiBase,
    shops: cfg.shops.map((s) => ({ ...s, token: cfg.staff_tokens[s.id] })),
    adminToken: cfg.admin_token,
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
  private readonly featureCache = new Map<string, Promise<{ testMessages: boolean }>>();

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
      replyToCustomer: (shop, id, body, clientId) =>
        call<StaffReplyResult>(s.apiBase, `/v1/shops/${shop}/conversations/${id}/reply`, {
          method: "POST",
          headers: { ...this.staff(shop), "Content-Type": "application/json" },
          body: JSON.stringify({ body, client_id: clientId }),
        }),
      sendAsCustomer: (shop, message) =>
        call<TestMessageResult>(s.apiBase, `/v1/shops/${shop}/test-messages`, {
          method: "POST",
          headers: { ...this.staff(shop), "Content-Type": "application/json" },
          body: JSON.stringify(message),
        }),
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

  customers(shop: string) {
    return this.get<CustomerList>(shop, `/v1/shops/${shop}/customers`);
  }

  customer(shop: string, id: string) {
    return this.get<CustomerHistory>(shop, `/v1/shops/${shop}/customers/${encodeURIComponent(id)}`);
  }

  /** Asked once per shop per session: the deployment's switches don't change while you work. */
  features(shop: string) {
    let f = this.featureCache.get(shop);
    if (!f) {
      f = this.get<{ features?: { test_messages?: boolean } }>(shop, "/v1/me").then((me) => ({
        testMessages: me.features?.test_messages === true,
      }));
      f.catch(() => this.featureCache.delete(shop));
      this.featureCache.set(shop, f);
    }
    return f;
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
