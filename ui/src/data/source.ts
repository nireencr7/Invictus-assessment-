import type {
  ActivityItem,
  ConversationDetail,
  CustomerHistory,
  CustomerList,
  DevJob,
  DevLogLine,
  DevRun,
  DevStatus,
  EvalSummary,
  Inbox,
  LlmCall,
  Shop,
  Spend,
  WorkOrderList,
  WorkOrderStatus,
  StaffReplyResult,
  TestMessage,
  TestMessageResult,
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
  customers(shop: string): Promise<CustomerList>;
  customer(shop: string, id: string): Promise<CustomerHistory>;
  /** What this deployment lets staff do beyond reading (e.g. send test messages). */
  features(shop: string): Promise<{ testMessages: boolean }>;
  evalSummary(): Promise<EvalSummary | null>;
  /** The worker's queue, a step button and the server log. Null unless the API runs with DEV_MODE=true. */
  devTools(): Promise<DevTools | null>;
  actions: Actions | null;
}

/** Dev only. Across all shops: the queue is one table, and these routes don't exist in production. */
export interface DevTools {
  status(): Promise<DevStatus>;
  jobs(scope: "open" | "all"): Promise<DevJob[]>;
  /** One turn of the worker loop: claim the next ready job and run the whole pipeline on it. */
  runOnce(): Promise<DevRun>;
  logs(): Promise<DevLogLine[]>;
}

export interface Actions {
  retryAi(shop: string, conversationId: string): Promise<void>;
  setWorkOrderStatus(shop: string, id: string, status: Exclude<WorkOrderStatus, "cancel_requested">): Promise<void>;
  /** Present only with an admin token. */
  setCap?: (shop: string, usd: string) => Promise<{ released: number }>;
  /** Staff write to the customer; answering a conversation that needed a person hands it back. */
  replyToCustomer(shop: string, conversationId: string, body: string, clientId: string): Promise<StaffReplyResult>;
  /** Staff test tool: send a message as a customer of this shop. Only works where features().testMessages. */
  sendAsCustomer(shop: string, message: TestMessage): Promise<TestMessageResult>;
}

export class SourceError extends Error {
  constructor(
    message: string,
    readonly status?: number,
  ) {
    super(message);
  }
}
