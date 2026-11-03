// Display helpers. Money arrives already formatted or as decimal strings from the API;
// nothing here does arithmetic that could change a price.

export const human = (s: string | null | undefined) => (s ?? "").replaceAll("_", " ");

const REASONS: Record<string, string> = {
  ai_unavailable: "AI unavailable",
  ai_configuration_error: "AI configuration error",
  invalid_model_output: "Model output failed validation",
  model_refused: "Model declined",
  held_budget: "Held at spend cap",
  wrong_shop: "Asked which shop",
  missing_date: "Asked for a date",
  missing_issue: "Asked what's wrong",
  ambiguous_request: "Asked to clarify",
  price_quote: "Price quoted",
  cancellation_needs_match: "Cancellation: match needed",
  customer_wrote_after_conversation_closed: "Wrote after close",
  not_a_repair_request: "Not a repair request",
  shop_unclear_after_follow_up: "Shop still unclear",
  no_service_after_follow_up: "Still no clear repair",
  price_question_unclear: "Unclear price question",
  staff_replied: "Staff replied",
};

export const reasonLabel = (r: string | null | undefined) => (r ? (REASONS[r] ?? human(r)) : "");

const FLAGS: Record<string, string> = {
  possible_prompt_injection: "Possible prompt injection",
  estimate_provisional_until_inspection: "Provisional until inspection",
  wheel_not_specified_estimate_assumes_one: "Wheel not stated: 1 assumed",
  no_date_after_follow_up: "No date after follow-up",
  customer_confirmed_shop_after_wrong_shop_question: "Confirmed shop after asking",
};

export const flagLabel = (f: string) =>
  FLAGS[f] ?? (f.startsWith("customer_language_") ? `Customer wrote in ${f.slice(18).toUpperCase()}` : human(f));

const EVENTS: Record<string, string> = {
  received: "Message received",
  duplicate_delivery: "Duplicate delivery ignored",
  llm_call: "Model call",
  llm_transient_error: "Model call failed (will retry)",
  validation_failed: "Model output rejected",
  follow_up_asked: "Follow-up question sent",
  work_order_created: "Work order created",
  work_order_exists: "Work order already existed",
  price_quoted: "Price quote sent",
  cancellation_requested: "Cancellation requested",
  redirected: "Pointed to another shop",
  escalated: "Handed to staff",
  held_budget: "Held at spend cap",
  budget_warning: "Spend at 80% of cap",
  budget_exhausted: "Spend cap reached",
  budget_cap_changed: "Spend cap changed",
  work_order_status: "Work order status changed",
  staff_replied: "Staff replied",
};

export const eventLabel = (t: string) => EVENTS[t] ?? human(t);

const MESSAGE_KINDS: Record<string, string> = {
  ack: "Acknowledgement",
  follow_up: "Follow-up question",
  summary: "Work order summary",
  price_quote: "Price quote",
  cancellation_ack: "Cancellation received",
  redirect: "Pointed to another shop",
  handoff: "Passed to the team",
  staff_reply: "Staff reply",
};

/** What a message we sent the customer was, in words. */
export const messageKindLabel = (k: string) => MESSAGE_KINDS[k] ?? human(k);

export type Tone = "ok" | "warn" | "bad" | "info" | "neutral";

export function eventTone(t: string, detail: Record<string, unknown> = {}): Tone {
  if (t === "llm_call") return detail.valid === false ? "warn" : "neutral";
  if (["escalated", "budget_exhausted", "llm_transient_error"].includes(t)) return "bad";
  if (["validation_failed", "held_budget", "budget_warning", "cancellation_requested", "duplicate_delivery"].includes(t))
    return "warn";
  if (["work_order_created", "price_quoted", "staff_replied"].includes(t)) return "ok";
  return "info";
}

export function callTone(status: string): Tone {
  if (status === "ok") return "ok";
  if (status === "invalid_output" || status === "reserved") return "warn";
  return "bad";
}

/** What one worker run ended with (pipeline.py return values; escalations are "escalated:<reason>"). */
export function outcomeTone(outcome: string): Tone {
  if (["work_order_created", "follow_up_asked", "price_quoted", "cancellation_requested", "redirected"].includes(outcome))
    return "ok";
  if (["dead_after_crash", "crash_retry", "lease_lost", "missing_message"].includes(outcome)) return "bad";
  if (outcome.startsWith("escalated:") || ["retry_scheduled", "held_budget", "deferred_breaker_open"].includes(outcome))
    return "warn";
  return "info";
}

export function jobTone(status: string): Tone {
  return ({ done: "ok", queued: "info", running: "info", parked: "warn", dead: "bad" } as Record<string, Tone>)[status] ?? "neutral";
}

/** "$0.0160" for sub-dollar model costs, "$1.23" otherwise. */
export function usd(value: string | number | null | undefined): string {
  if (value === null || value === undefined || value === "") return "—";
  const n = typeof value === "number" ? value : Number(value);
  if (!Number.isFinite(n)) return "—";
  return n !== 0 && Math.abs(n) < 1 ? `$${n.toFixed(4)}` : `$${n.toFixed(2)}`;
}

export function centsToUsd(cents: number | null | undefined): string {
  if (cents === null || cents === undefined) return "—";
  const whole = Math.trunc(cents / 100);
  const frac = String(Math.abs(cents) % 100).padStart(2, "0");
  return `$${whole.toLocaleString("en-US")}.${frac}`;
}

const dt = new Intl.DateTimeFormat("en-GB", { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" });

export function when(iso: string | null | undefined): string {
  if (!iso) return "";
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? iso : dt.format(d);
}

export function tokens(n: number | null | undefined): string {
  if (n === null || n === undefined) return "—";
  return n >= 10_000 ? `${(n / 1000).toFixed(1)}k` : n.toLocaleString("en-US");
}

export function ratio(part: string | number, whole: string | number): number {
  const p = Number(part);
  const w = Number(whole);
  return w > 0 ? Math.min(1, Math.max(0, p / w)) : 0;
}
