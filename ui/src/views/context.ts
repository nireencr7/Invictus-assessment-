import type { DataSource } from "../data/source";

export type View = "overview" | "orders" | "inbox" | "activity" | "spend" | "results";

export interface Ctx {
  source: DataSource;
  shopId: string;
  shopName: (id: string) => string;
  /** Poll interval for live data; undefined for a snapshot, which never changes. */
  poll?: number;
  openConversation: (shopId: string, conversationId: string) => void;
  go: (view: View, shopId?: string) => void;
  toast: (message: string) => void;
}
