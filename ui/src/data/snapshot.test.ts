import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";
import type { Snapshot } from "../types";
import { SnapshotSource } from "./snapshot";

const snapshot = JSON.parse(readFileSync(new URL("../../public/demo-snapshot.json", import.meta.url), "utf8")) as Snapshot;
const source = new SnapshotSource(snapshot);

describe("snapshot source", () => {
  it("covers all three shops", () => {
    expect(source.shops().map((s) => s.id)).toEqual(["rb-downtown", "rb-northside", "rb-lakeshore"]);
  });

  it("filters open work orders the same way the API does", async () => {
    const open = await source.workOrders("rb-lakeshore", "open");
    expect(open.work_orders.every((w) => w.status === "open" || w.status === "cancel_requested")).toBe(true);
    expect(open.count).toBe(open.work_orders.length);
  });

  it("keeps each shop's data to itself", async () => {
    for (const shop of source.shops()) {
      const orders = await source.workOrders(shop.id, "all");
      expect(orders.work_orders.length).toBeGreaterThan(0);
      for (const id of Object.keys(snapshot.byShop[shop.id].conversations)) {
        expect((await source.conversation(shop.id, id)).conversation.shop_id).toBe(shop.id);
      }
    }
    await expect(source.workOrders("rb-elsewhere", "all")).rejects.toThrow("Not found");
  });

  it("has demo customers, each with a history from their own shop only", async () => {
    const down = await source.customers("rb-downtown");
    expect(down.customers.map((c) => c.email)).toContain("jamie.lee@example.com");
    for (const shop of source.shops()) {
      for (const c of (await source.customers(shop.id)).customers) {
        const h = await source.customer(shop.id, c.id);
        const ids = new Set(Object.keys(snapshot.byShop[shop.id].conversations));
        expect(h.conversations.every((cv) => ids.has(cv.id))).toBe(true);
        expect(h.timeline.length).toBeGreaterThan(0);
      }
    }
    expect(await source.features()).toEqual({ testMessages: false });
  });

  it("reads snapshots exported before customers existed", async () => {
    const old = structuredClone(snapshot);
    delete old.byShop["rb-downtown"].customers;
    expect((await new SnapshotSource(old).customers("rb-downtown")).customers).toEqual([]);
  });

  it("has no dev tools: the worker page is for a local API only", async () => {
    expect(await source.devTools()).toBeNull();
  });

  it("never exports a customer's reply token", () => {
    expect(JSON.stringify(snapshot)).not.toContain("reply_token");
  });

  it("estimates equal the sum of their lines", async () => {
    for (const shop of source.shops()) {
      for (const w of (await source.workOrders(shop.id, "all")).work_orders) {
        const sum = w.lines.reduce((acc, l) => acc + Math.round(Number(l.line_total.replace(/[$,]/g, "")) * 100), 0);
        expect(sum).toBe(w.estimate.cents);
      }
    }
  });
});
