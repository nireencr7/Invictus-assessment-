import { Chip, Empty, ErrorNote, Loading, SpendMeter, Stat, spendTone } from "../components/ui";
import { centsToUsd, reasonLabel, usd, when } from "../lib/format";
import { useResource } from "../lib/useResource";
import type { Ctx } from "./context";

export function Overview({ ctx }: { ctx: Ctx }) {
  const shops = ctx.source.shops();
  const res = useResource(
    () =>
      Promise.all(
        shops.map(async (shop) => {
          const [orders, inbox, spend] = await Promise.all([
            ctx.source.workOrders(shop.id, "open"),
            ctx.source.inbox(shop.id),
            ctx.source.spend(shop.id),
          ]);
          return { shop, orders, inbox, spend };
        }),
      ),
    `overview:${shops.map((s) => s.id).join(",")}`,
    ctx.poll,
  );

  if (res.error && !res.data) return <ErrorNote error={res.error} />;
  if (!res.data) return <Loading />;

  const all = res.data;
  const openOrders = all.reduce((n, s) => n + s.orders.work_orders.filter((w) => w.status === "open").length, 0);
  const openCents = all.reduce(
    (n, s) => n + s.orders.work_orders.filter((w) => w.status === "open").reduce((m, w) => m + w.estimate.cents, 0),
    0,
  );
  const attention = all.flatMap((s) => s.inbox.items.filter((i) => i.needs_action).map((i) => ({ ...i, shop: s.shop })));
  const spent = all.reduce((n, s) => n + Number(s.spend.spent_usd), 0);

  return (
    <>
      <div className="stats">
        <Stat label="Open work orders" value={openOrders} sub={`across ${all.length} shop${all.length === 1 ? "" : "s"}`} />
        <Stat label="Open estimate total" value={centsToUsd(openCents)} sub="catalog prices, computed by the API" />
        <Stat label="Needs a person" value={attention.length} sub="handed to staff or held at a cap" />
        <Stat label="Model spend this month" value={usd(spent)} sub={all[0] ? all[0].spend.month.slice(0, 7) : ""} />
      </div>

      <div className="shops">
        {all.map(({ shop, orders, inbox, spend }) => {
          const open = orders.work_orders.filter((w) => w.status === "open");
          const high = open.filter((w) => w.urgency === "high").length;
          const needs = inbox.items.filter((i) => i.needs_action).length;
          return (
            <button key={shop.id} className="card pad shopcard" style={{ textAlign: "left", cursor: "pointer" }}
                    onClick={() => ctx.go("orders", shop.id)}>
              <header>
                <h3>{shop.name.replace("Riverbend Cycles ", "")}</h3>
                <Chip tone={spendTone(spend.state)}>spend {spend.state}</Chip>
              </header>
              <div className="kv">
                <div><b>{open.length}</b><span>open orders</span></div>
                <div><b>{high}</b><span>high urgency</span></div>
                <div><b style={{ color: needs ? "var(--bad)" : undefined }}>{needs}</b><span>need a person</span></div>
              </div>
              <div>
                <div className="row" style={{ justifyContent: "space-between", fontSize: 12.5, marginBottom: 6 }}>
                  <span className="muted">Open estimates</span>
                  <b className="num">{centsToUsd(open.reduce((m, w) => m + w.estimate.cents, 0))}</b>
                </div>
                <SpendMeter spend={spend} />
                <div className="row muted num" style={{ justifyContent: "space-between", fontSize: 12, marginTop: 4 }}>
                  <span>{usd(spend.spent_usd)} spent</span>
                  <span>cap {usd(spend.cap_usd)}</span>
                </div>
              </div>
            </button>
          );
        })}
      </div>

      <section className="card">
        <div className="section-head" style={{ padding: "14px 16px 6px" }}>
          <h2>Needs a person</h2>
          <span className="muted" style={{ fontSize: 13 }}>Everything the system handed back instead of guessing</span>
        </div>
        {attention.length === 0 ? (
          <Empty>Nothing is waiting on staff.</Empty>
        ) : (
          <div className="list">
            {attention.map((i) => (
              <div key={i.conversation_id} className="item" role="button" tabIndex={0}
                   onClick={() => ctx.openConversation(i.shop.id, i.conversation_id)}
                   onKeyDown={(e) => e.key === "Enter" && ctx.openConversation(i.shop.id, i.conversation_id)}>
                <div className="body">{i.body}</div>
                <Chip tone={i.message_status === "held_budget" ? "warn" : "bad"}>
                  {reasonLabel(i.message_status === "held_budget" ? "held_budget" : i.state_reason)}
                </Chip>
                <div className="meta">
                  <span>{i.shop.name.replace("Riverbend Cycles ", "")}</span>·<span className="mono">{i.external_id}</span>·
                  <span>{when(i.updated_at)}</span>
                </div>
              </div>
            ))}
          </div>
        )}
      </section>
    </>
  );
}
