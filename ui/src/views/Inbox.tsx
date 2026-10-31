import { useState } from "react";
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
  const send = ctx.source.actions?.sendMessage;

  return (
    <>
      {send ? <Composer ctx={ctx} send={send} onSent={res.reload} /> : null}
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

function Composer({ ctx, send, onSent }: {
  ctx: Ctx;
  send: NonNullable<NonNullable<Ctx["source"]["actions"]>["sendMessage"]>;
  onSent: () => void;
}) {
  const [body, setBody] = useState("");
  const [channel, setChannel] = useState<"email" | "web">("email");
  const [busy, setBusy] = useState(false);
  return (
    <form className="card pad" style={{ display: "grid", gap: 10 }}
          onSubmit={async (e) => {
            e.preventDefault();
            if (!body.trim()) return;
            setBusy(true);
            try {
              const r = await send(ctx.shopId, body.trim(), channel);
              ctx.toast(r.duplicate ? "Duplicate: ignored" : "Accepted (202). Processing in the background.");
              setBody("");
              onSent();
            } catch (err) {
              ctx.toast(`Rejected: ${err instanceof Error ? err.message : String(err)}`);
            } finally {
              setBusy(false);
            }
          }}>
      <div className="section-head">
        <h2>Simulate an inbound message</h2>
        <span className="muted" style={{ fontSize: 13 }}>Posts to {ctx.shopName(ctx.shopId)} as the email gateway would (dev only)</span>
      </div>
      <textarea id="composer-body" className="input" value={body} onChange={(e) => setBody(e.target.value)}
                placeholder="My rear brake squeals and the gears skip on hills, can I bring it Saturday?" />
      <div className="row">
        <select id="composer-channel" className="input" style={{ width: "auto" }} value={channel}
                onChange={(e) => setChannel(e.target.value as "email" | "web")}>
          <option value="email">email</option>
          <option value="web">web form</option>
        </select>
        <button className="btn primary" disabled={busy || !body.trim()}>Send to {ctx.shopName(ctx.shopId).replace("Riverbend Cycles ", "")}</button>
      </div>
    </form>
  );
}
