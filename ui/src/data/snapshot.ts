import type { ConversationDetail, CustomerHistory, CustomerList, Snapshot, WorkOrderList } from "../types";
import { SourceError, type DataSource } from "./source";

const OPEN = new Set(["open", "cancel_requested"]);

/** Read-only view over an exported run. Mirrors the API's own filtering so views behave the same. */
export class SnapshotSource implements DataSource {
  readonly kind = "snapshot" as const;
  readonly actions = null;

  constructor(readonly snapshot: Snapshot) {}

  static async load(url: string): Promise<SnapshotSource> {
    const r = await fetch(url, { cache: "no-store" });
    if (!r.ok) throw new SourceError(`Couldn't load the snapshot (${r.status})`, r.status);
    return new SnapshotSource((await r.json()) as Snapshot);
  }

  private shop(id: string) {
    const data = this.snapshot.byShop[id];
    if (!data) throw new SourceError("Not found", 404);
    return data;
  }

  shops() {
    return this.snapshot.shops;
  }

  async workOrders(shop: string, scope: "open" | "all"): Promise<WorkOrderList> {
    const all = this.shop(shop).workOrders;
    if (scope === "all") return all;
    const work_orders = all.work_orders.filter((w) => OPEN.has(w.status));
    return { ...all, count: work_orders.length, work_orders };
  }

  async inbox(shop: string) {
    return this.shop(shop).inbox;
  }

  async spend(shop: string) {
    return this.shop(shop).spend;
  }

  async llmCalls(shop: string) {
    return this.shop(shop).llmCalls.items;
  }

  async activity(shop: string) {
    return this.shop(shop).activity.items;
  }

  async conversation(shop: string, id: string): Promise<ConversationDetail> {
    const c = this.shop(shop).conversations[id];
    if (!c) throw new SourceError("Not found", 404);
    return c;
  }

  async customers(shop: string): Promise<CustomerList> {
    return this.shop(shop).customers ?? { shop_id: shop, count: 0, conversations_without_email: 0, customers: [] };
  }

  async customer(shop: string, id: string): Promise<CustomerHistory> {
    const c = this.shop(shop).customerHistory?.[id];
    if (!c) throw new SourceError("Not found", 404);
    return c;
  }

  async features() {
    return { testMessages: false };
  }

  async evalSummary() {
    return this.snapshot.eval;
  }
}
