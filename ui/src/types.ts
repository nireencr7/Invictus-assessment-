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
