import { Chip, Empty, ErrorNote, Loading } from "../components/ui";
import { reasonLabel, when } from "../lib/format";
import { useResource } from "../lib/useResource";
import type { InboxItem } from "../types";
import type { Ctx } from "./context";

const GROUPS: { title: string; hint: string; test: (i: InboxItem) => boolean }[] = [
  { title: "Needs a person", hint: "Handed to staff, or held at the spend cap", test: (i) => i.needs_action },
  { title: "Waiting on the customer", hint: "We asked one question", test: (i) => !i.needs_action && i.state === "awaiting_customer" },
  { title: "Being processed", hint: "Queued or with the model", test: (i) => !i.needs_action && i.state === "processing" },
  { title: "Answered without a work order", hint: "Price questions", test: (i) => !i.needs_action && i.state === "answered" },
];

export function Inbox({ ctx }: { ctx: Ctx }) {
  const res = useResource(() => ctx.source.inbox(ctx.shopId), `inbox:${ctx.shopId}`, ctx.poll);
  const features = useResource(() => ctx.source.features(ctx.shopId), `features:${ctx.shopId}`);

  return (
    <>
      {features.data?.testMessages ? (
        <div className="banner info row" style={{ justifyContent: "space-between" }}>
          <span>Want to try the workflow? Send a message as a customer and watch it arrive here.</span>
          <button className="btn" onClick={() => ctx.go("customers")}>Send a test message</button>
        </div>
      ) : null}
      {res.error && !res.data ? <ErrorNote error={res.error} /> : null}
      {!res.data ? (
        res.error ? null : <Loading />
      ) : res.data.items.length === 0 ? (
        <section className="card"><Empty>Nothing in flight. Every message became a work order or was answered.</Empty></section>
      ) : (
        GROUPS.map((g) => {
          const items = res.data!.items.filter(g.test);
          if (!items.length) return null;
          return (
            <section key={g.title} className="card">
              <div className="section-head" style={{ padding: "14px 16px 6px" }}>
                <h2>{g.title} <span className="muted num" style={{ fontWeight: 400 }}>{items.length}</span></h2>
                <span className="muted" style={{ fontSize: 13 }}>{g.hint}</span>
              </div>
              <div className="list">
                {items.map((i) => <Row key={i.conversation_id} ctx={ctx} item={i} />)}
              </div>
            </section>
          );
        })
      )}
    </>
  );
}

function Row({ ctx, item: i }: { ctx: Ctx; item: InboxItem }) {
  const held = i.message_status === "held_budget";
  const reason = held ? "held_budget" : i.state_reason;
  return (
    <div className="item" role="button" tabIndex={0} onClick={() => ctx.openConversation(ctx.shopId, i.conversation_id)}
         onKeyDown={(e) => e.key === "Enter" && ctx.openConversation(ctx.shopId, i.conversation_id)}>
      <div className="body">{i.body}</div>
      {reason ? <Chip tone={i.needs_action ? (held ? "warn" : "bad") : "info"}>{reasonLabel(reason)}</Chip> : <span />}
      <div className="meta">
        <span className="mono">{i.external_id}</span>
        {i.customer_name ? <>·<span>{i.customer_name}</span></> : null}·<span>{when(i.updated_at)}</span>
      </div>
    </div>
  );
}
