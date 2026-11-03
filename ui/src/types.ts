// Shapes returned by the Riverbend API (src/riverbend/api.py). Kept by hand and small on purpose:
// the console reads; it never computes a price or a date the API didn't give it.

export interface Shop {
  id: string;
  name: string;
}

export interface Line {
  code: string;
  name: string;
  wheel: string;
  quantity: number;
  unit_price: string;
  line_total: string;
  requires_inspection: boolean;
}

export type WorkOrderStatus = "open" | "cancel_requested" | "cancelled" | "completed";
export type Urgency = "low" | "normal" | "high";

export interface WorkOrder {
  id: string;
  status: WorkOrderStatus;
  created_at: string;
  conversation_id: string;
  customer_name: string | null;
  bike: { type: string; count: number; brakes: string };
  issues: { summary: string; evidence: string }[];
  urgency: Urgency;
  requested: null | {
    earliest: string;
    latest: string;
    time_of_day: string;
    time: string | null;
    phrase: string | null;
    label: string;
  };
  lines: Line[];
  estimate: { cents: number; formatted: string; currency: string; provisional: boolean };
  review_reasons: string[];
  notes_for_staff: string | null;
  model: string;
  catalog_version: string;
}

export interface WorkOrderList {
  shop_id: string;
  count: number;
  open_estimate_total: string;
  work_orders: WorkOrder[];
}

export interface InboxItem {
  conversation_id: string;
  state: string;
  state_reason: string | null;
  customer_name: string | null;
  follow_ups_asked: number;
  updated_at: string;
  external_id: string;
  body: string;
  message_status: string;
  outcome: string | null;
  created_at: string;
  needs_action: boolean;
}

export interface Inbox {
  shop_id: string;
  count: number;
  items: InboxItem[];
}

export interface Spend {
  shop_id: string;
  month: string;
  cap_usd: string;
  spent_usd: string;
  reserved_usd: string;
  remaining_usd: string;
  state: "ok" | "warning" | "exhausted";
  calls: number;
  input_tokens: number;
  output_tokens: number;
}

export interface LlmCall {
  id: number;
  purpose: "extract" | "repair";
  status: string;
  provider: string;
  model: string;
  reserved_usd: string;
  cost_usd: string | null;
  input_tokens: number | null;
  output_tokens: number | null;
  latency_ms: number | null;
  error: string | null;
  created_at: string;
  external_id: string;
  conversation_id: string;
}

export interface ActivityItem {
  id: number;
  type: string;
  detail: Record<string, unknown>;
  created_at: string;
  conversation_id: string | null;
  external_id: string | null;
}

export interface ConversationDetail {
  conversation: {
    id: string;
    shop_id: string;
    channel: string;
    state: string;
    state_reason: string | null;
    follow_ups_asked: number;
    customer_name: string | null;
    created_at: string;
    updated_at: string;
    /** Set when the message came from an email address (see Customers). */
    customer_id?: string | null;
    sender?: string | null;
  };
  inbound: { external_id: string; body: string; received_at: string; status: string; outcome: string | null; created_at: string }[];
  outbound: { kind: string; body: string; created_at: string }[];
  events: { type: string; detail: Record<string, unknown>; created_at: string }[];
  llm_calls: {
    purpose: string;
    status: string;
    model: string;
    cost_usd: string | null;
    input_tokens: number | null;
    output_tokens: number | null;
    latency_ms: number | null;
    error: string | null;
    created_at: string;
  }[];
  customerView?: { status: string; messages: { kind: string; body: string; created_at: string }[] };
}

export interface Customer {
  id: string;
  email: string;
  name: string | null;
  first_seen_at: string;
  last_activity: string;
  conversations: number;
  awaiting_reply: number;
  needs_staff: number;
  open_orders: number;
  open_estimate: string;
}

export interface CustomerList {
  shop_id: string;
  count: number;
  conversations_without_email: number;
  customers: Customer[];
}

export type TimelineEntry =
  | { kind: "customer_message"; at: string; conversation_id: string; external_id: string; channel: string; body: string; status: string }
  | { kind: "shop_message"; at: string; conversation_id: string; message_kind: string; body: string }
  | { kind: "event"; at: string; conversation_id: string | null; type: string; detail: Record<string, unknown>; external_id: string | null };

export interface CustomerHistory {
  customer: Customer;
  model_calls: number;
  model_cost_usd: string;
  conversations: {
    id: string;
    channel: string;
    state: string;
    state_reason: string | null;
    follow_ups_asked: number;
    customer_name: string | null;
    first_message: string | null;
    created_at: string;
    updated_at: string;
    work_order: null | { id: string; status: WorkOrderStatus; estimate: string };
  }[];
  timeline: TimelineEntry[];
}

export interface StaffReplyResult {
  sent: boolean;
  duplicate: boolean;
  state: string;
  /** True when the reply took the conversation off the "needs a person" list. */
  resolved: boolean;
}

export interface TestMessage {
  customer_email: string;
  customer_name?: string;
  body: string;
  channel: "email" | "web";
  /** Conversation id to reply in; omit for a new request. */
  reply_to?: string;
}

export interface TestMessageResult {
  external_id: string;
  conversation_id: string;
  customer_id: string | null;
  reply: boolean;
}

export interface EvalRow {
  first_id: string;
  shop_id: string;
  state: string;
  outcome: string;
  follow_up: string | null;
  codes: Record<string, number>;
  estimate_cents: number | null;
  window: [string, string] | null;
  urgency: string | null;
  review: string[];
  wo_status: string | null;
  state_reason: string | null;
  quoted?: string[] | null;
  ok_outcome?: boolean;
  ok_services?: boolean;
  ok_quantities?: boolean | null;
  ok_estimate?: boolean | null;
  ok_window?: boolean | null;
  ok_follow_up?: boolean;
}

export interface EvalRun {
  provider: string;
  model: string;
  simulated_spend: boolean;
  messages_received: number;
  replies: number;
  duplicates: number;
  conversations: number;
  work_orders: number;
  cancel_requested: number;
  calls: number;
  calls_by_status: Record<string, number>;
  input_tokens: number;
  output_tokens: number;
  cost_usd: string;
  retries: number;
  events: Record<string, number>;
  rows: EvalRow[];
  injected?: Record<string, number>;
}

export interface EvalSummary {
  source: string;
  baseline: EvalRun | null;
  chaos: EvalRun | null;
  budget: null | {
    held: number;
    released: number;
    heldExamples: string[];
    spendAtCap: Spend;
    workOrdersAfterRaise: number;
  };
  outage: null | {
    acks: number;
    workOrdersDuring: number;
    costDuring: string;
    handedToStaff: number;
    retried: number;
    workOrdersAfterRecovery: number;
  };
}

export interface ShopData {
  workOrders: WorkOrderList;
  inbox: Inbox;
  spend: Spend;
  llmCalls: { shop_id: string; items: LlmCall[] };
  activity: { shop_id: string; items: ActivityItem[] };
  conversations: Record<string, ConversationDetail>;
  /** Absent in snapshots exported before customers existed. */
  customers?: CustomerList;
  customerHistory?: Record<string, CustomerHistory>;
}

export interface Snapshot {
  generatedAt: string;
  provider: string;
  model: string;
  simulatedSpend: boolean;
  note: string;
  shops: Shop[];
  byShop: Record<string, ShopData>;
  eval: EvalSummary | null;
}

// Dev only (DEV_MODE=true): the job queue, one turn of the worker, and the server log.
export type JobStatus = "queued" | "running" | "done" | "parked" | "dead";

export interface DevJobMessage {
  external_id: string;
  conversation_id: string;
  body: string;
  status: string;
  outcome: string | null;
}

export interface DevJob {
  id: number;
  shop_id: string;
  status: JobStatus;
  attempts: number;
  ready: boolean;
  run_after: string;
  locked_until: string | null;
  park_reason: string | null;
  last_error: string | null;
  created_at: string;
  updated_at: string;
  message: DevJobMessage | null;
}

export interface DevModelCall {
  purpose: string;
  status: string;
  provider: string;
  model: string;
  reserved_usd: string;
  cost_usd: string | null;
  input_tokens: number | null;
  output_tokens: number | null;
  latency_ms: number | null;
  error: string | null;
  raw_output: unknown;
}

export interface DevDecided {
  id: string;
  estimate_cents: number;
  urgency: string;
  requested_earliest: string | null;
  requested_latest: string | null;
  requested_time_of_day: string | null;
  review_reasons: string[];
  lines: { service_code: string; wheel: string; quantity: number; unit_price_cents: number; line_total_cents: number }[];
}

export type DevRun =
  | {
      ran: false;
      detail: string;
      queued_for_later: { id: number; shop_id: string; run_after: string; attempts: number; last_error: string | null }[];
    }
  | {
      ran: true;
      job: { id: number; shop_id: string; attempt: number };
      message: DevJobMessage | null;
      outcome: string;
      model_calls?: DevModelCall[];
      events?: { type: string; detail: Record<string, unknown> }[];
      messages_sent?: { kind: string; body: string }[];
      conversation?: { id: string; state: string; state_reason: string | null; follow_ups_asked: number };
      /** The validated model output, present when this run created a work order. */
      model_output?: Record<string, unknown>;
      code_decided?: DevDecided;
      job_after: { status: JobStatus; attempts: number; run_after: string; park_reason: string | null; last_error: string | null };
    };

export interface DevLogLine {
  seq: number;
  time: string;
  level: string;
  logger: string;
  message: string;
}

export interface DevStatus {
  embeddedWorker: boolean;
  /** Worker threads running right now (1-3, sized by the backlog); 0 in step mode. */
  workerThreads: number;
  provider: string;
  model: string;
  breaker: string;
}
