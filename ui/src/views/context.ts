import type { DataSource } from "../data/source";

export type View = "overview" | "orders" | "inbox" | "activity" | "spend" | "results";

export interface Ctx {
  source: DataSource;
  shopId: string;
  shopName: (id: string) => string;
  /** Auto-refresh interval for live data. Undefined means no auto-refresh: always for a snapshot,
   *  which never changes, and for now for live data too (staff press Refresh instead). */
  poll?: number;
  openConversation: (shopId: string, conversationId: string) => void;
  go: (view: View, shopId?: string) => void;
  toast: (message: string) => void;
}
