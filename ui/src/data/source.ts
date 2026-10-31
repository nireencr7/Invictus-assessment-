import type {
  ActivityItem,
  ConversationDetail,
  EvalSummary,
  Inbox,
  LlmCall,
  Shop,
  Spend,
  WorkOrderList,
  WorkOrderStatus,
} from "../types";

/**
 * Everything a view can ask for. Two implementations:
 *  - ApiSource: the live API, scoped by the staff tokens in this session;
 *  - SnapshotSource: a read-only export of a real run (ui/public/demo-snapshot.json).
 * Views never know which one they have, except to hide actions a snapshot can't perform.
 */
export interface DataSource {
  readonly kind: "live" | "snapshot";
  shops(): Shop[];
  workOrders(shop: string, scope: "open" | "all"): Promise<WorkOrderList>;
  inbox(shop: string): Promise<Inbox>;
  spend(shop: string): Promise<Spend>;
  llmCalls(shop: string): Promise<LlmCall[]>;
  activity(shop: string): Promise<ActivityItem[]>;
  conversation(shop: string, id: string): Promise<ConversationDetail>;
  evalSummary(): Promise<EvalSummary | null>;
  actions: Actions | null;
}

export interface Actions {
  retryAi(shop: string, conversationId: string): Promise<void>;
  setWorkOrderStatus(shop: string, id: string, status: Exclude<WorkOrderStatus, "cancel_requested">): Promise<void>;
  /** Present only with an admin token. */
  setCap?: (shop: string, usd: string) => Promise<{ released: number }>;
  /** Present only with an ingest key (local development). */
  sendMessage?: (shop: string, body: string, channel: "email" | "web", replyToken?: string) => Promise<{ duplicate: boolean }>;
}

export class SourceError extends Error {
  constructor(
    message: string,
    readonly status?: number,
  ) {
    super(message);
  }
}
