import { useEffect, useRef, useState } from "react";
import { Chip, ErrorNote, Loading, Ticket } from "../components/ui";
import { callTone, eventLabel, eventTone, human, reasonLabel, tokens, usd, when } from "../lib/format";
import { useResource } from "../lib/useResource";
import { describe } from "./Activity";
import type { Ctx } from "./context";

const AI_REASONS = new Set(["ai_unavailable", "invalid_model_output", "model_refused", "ai_configuration_error"]);

// Mirrors the API's customer route, so the drawer can say what the customer currently sees.
const CUSTOMER_STATUS: Record<string, string> = {
  processing: "We're reviewing your request.",
  awaiting_customer: "We've asked you a question — just reply to continue.",
  work_order_created: "Your request is with the workshop team.",
  answered: "We've answered your question.",
  cancellation_requested: "Your cancellation is being processed.",
  redirected: "Please contact the other shop directly.",
  needs_staff: "A team member is reviewing your request personally.",
  closed: "This conversation is closed.",
};

export function Conversation({ ctx, shopId, conversationId, onClose }: {
  ctx: Ctx;
  shopId: string;
  conversationId: string;
  onClose: () => void;
}) {
  const closeRef = useRef<HTMLButtonElement>(null);
  const [busy, setBusy] = useState(false);
  const res = useResource(
    async () => {
      const [detail, orders] = await Promise.all([
        ctx.source.conversation(shopId, conversationId),
        ctx.source.workOrders(shopId, "all"),
      ]);
      return { detail, order: orders.work_orders.find((w) => w.conversation_id === conversationId) ?? null };
    },
    `conv:${shopId}:${conversationId}`,
    ctx.poll,
  );

  useEffect(() => {
    closeRef.current?.focus();
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  const act = async (label: string, fn: () => Promise<unknown>) => {
    setBusy(true);
    try {
      await fn();
      ctx.toast(label);
      res.reload();
    } catch (e) {
      ctx.toast(`Couldn't do that: ${e instanceof Error ? e.message : String(e)}`);
    } finally {
      setBusy(false);
    }
  };

  const d = res.data?.detail;
  const order = res.data?.order;
  const actions = ctx.source.actions;
  const thread = d
    ? [
        ...d.inbound.map((m) => ({ at: m.created_at, side: "customer" as const, who: `Customer · ${m.external_id}`, body: m.body })),
        ...d.outbound.map((m) => ({ at: m.created_at, side: "shop" as const, who: `Sent to customer · ${human(m.kind)}`, body: m.body })),
      ].sort((a, b) => a.at.localeCompare(b.at))
    : [];

  return (
    <>
      <div className="scrim" onClick={onClose} />
      <aside className="drawer" role="dialog" aria-modal="true" aria-label="Conversation">
        <div className="drawer-head">
          <div style={{ flex: 1, minWidth: 0, display: "grid", gap: 6 }}>
            <h2>{d?.conversation.customer_name ?? "Customer"} · {ctx.shopName(shopId).replace("Riverbend Cycles ", "")}</h2>
            {d ? (
              <div className="chips">
                <Chip tone={d.conversation.state === "needs_staff" ? "bad" : d.conversation.state === "work_order_created" ? "ok" : "info"}>
                  {human(d.conversation.state)}
                </Chip>
                {d.conversation.state_reason ? <Chip tone="warn">{reasonLabel(d.conversation.state_reason)}</Chip> : null}
                <Chip>{d.conversation.channel}</Chip>
                <Chip>{d.conversation.follow_ups_asked}/1 follow-up used</Chip>
              </div>
            ) : null}
            {d?.conversation.customer_id ? (
              <div className="row" style={{ fontSize: 13 }}>
                <span className="muted mono">{d.conversation.sender}</span>
                <button className="btn small" onClick={() => ctx.openCustomer(shopId, d.conversation.customer_id!)}>Customer history</button>
              </div>
            ) : null}
          </div>
          <button ref={closeRef} className="btn" onClick={onClose}>Close</button>
        </div>

        <div className="drawer-body">
          {res.error && !res.data ? <ErrorNote error={res.error} /> : null}
          {!d ? (res.error ? null : <Loading />) : (
            <>
              {actions && (d.conversation.state === "needs_staff" && AI_REASONS.has(d.conversation.state_reason ?? "")) ? (
                <div className="banner info row" style={{ justifyContent: "space-between" }}>
                  <span>The AI couldn't finish this one. The customer has been acknowledged.</span>
                  <button className="btn primary" disabled={busy}
                          onClick={() => act("Re-queued for the AI", () => actions.retryAi(shopId, conversationId))}>
                    Retry AI
                  </button>
                </div>
              ) : null}

              {order ? (
                <>
                  <h4>Work order</h4>
                  <Ticket order={order} shopName={ctx.shopName(shopId)} />
                  {order.requested ? (
                    <div className="muted" style={{ fontSize: 13 }}>
                      Requested <b className="num">{order.requested.label}</b>
                      {order.requested.phrase ? <> from “{order.requested.phrase}”</> : null}
                    </div>
                  ) : null}
                  {order.notes_for_staff ? <div className="banner info">{order.notes_for_staff}</div> : null}
                  {actions && (order.status === "open" || order.status === "cancel_requested") ? (
                    <div className="row">
                      <button className="btn" disabled={busy}
                              onClick={() => act("Marked completed", () => actions.setWorkOrderStatus(shopId, order.id, "completed"))}>
                        Mark completed
                      </button>
                      <button className="btn danger" disabled={busy}
                              onClick={() => act("Work order cancelled", () => actions.setWorkOrderStatus(shopId, order.id, "cancelled"))}>
                        {order.status === "cancel_requested" ? "Confirm cancellation" : "Cancel order"}
                      </button>
                    </div>
                  ) : null}
                </>
              ) : null}

              <h4>Messages</h4>
              <div className="muted" style={{ fontSize: 13 }}>
                Customer sees: <i>{d.customerView?.status ?? CUSTOMER_STATUS[d.conversation.state] ?? human(d.conversation.state)}</i>
              </div>
              <div className="thread">
                {thread.map((m, i) => (
                  <div key={i} className={`bubble ${m.side}`}>
                    <span className="who">{m.who} · {when(m.at)}</span>
                    {m.body}
                  </div>
                ))}
              </div>

              <h4>Model calls</h4>
              {d.llm_calls.length === 0 ? <div className="muted" style={{ fontSize: 13 }}>None.</div> : (
                <div className="calls">
                  {d.llm_calls.map((c, i) => (
                    <div key={i} className="call">
                      <Chip tone={callTone(c.status)}>{human(c.status)}</Chip>
                      <span>{c.purpose} · <span className="muted">{c.model}</span></span>
                      <span className="num muted">{tokens(c.input_tokens)}/{tokens(c.output_tokens)} · {usd(c.cost_usd)}</span>
                      {c.error ? <span className="err">{c.error}</span> : null}
                    </div>
                  ))}
                </div>
              )}

              <h4>Timeline</h4>
              <div className="feed" style={{ marginInline: -16 }}>
                {d.events.map((e, i) => (
                  <div key={i} className="ev">
                    <time>{when(e.created_at)}</time>
                    <span className={`dot ${eventTone(e.type, e.detail)}`} />
                    <div className="what">{eventLabel(e.type)}{describe(e) ? <small>{describe(e)}</small> : null}</div>
                  </div>
                ))}
              </div>
            </>
          )}
        </div>
      </aside>
    </>
  );
}
