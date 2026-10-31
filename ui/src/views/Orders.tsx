import { useMemo, useState } from "react";
import { Chip, Empty, ErrorNote, Loading, Stat, urgencyTone } from "../components/ui";
import { centsToUsd, flagLabel, human } from "../lib/format";
import { useResource } from "../lib/useResource";
import type { WorkOrder } from "../types";
import type { Ctx } from "./context";

type Filter = "open" | "review" | "high" | "cancel" | "all";

const FILTERS: { id: Filter; label: string; test: (w: WorkOrder) => boolean }[] = [
  { id: "open", label: "Open", test: (w) => w.status === "open" || w.status === "cancel_requested" },
  { id: "review", label: "Needs review", test: (w) => w.review_reasons.some((r) => !r.startsWith("estimate_provisional")) },
  { id: "high", label: "High urgency", test: (w) => w.urgency === "high" },
  { id: "cancel", label: "Cancel requested", test: (w) => w.status === "cancel_requested" },
  { id: "all", label: "All", test: () => true },
];

export function Orders({ ctx }: { ctx: Ctx }) {
  const [filter, setFilter] = useState<Filter>("open");
  const [q, setQ] = useState("");
  const res = useResource(() => ctx.source.workOrders(ctx.shopId, "all"), `orders:${ctx.shopId}`, ctx.poll);

  const rows = useMemo(() => {
    const all = res.data?.work_orders ?? [];
    const f = FILTERS.find((x) => x.id === filter)!;
    const needle = q.trim().toLowerCase();
    return all.filter(f.test).filter((w) => {
      if (!needle) return true;
      const hay = [w.customer_name, w.bike.type, w.notes_for_staff, ...w.lines.map((l) => `${l.code} ${l.name}`),
                   ...w.issues.map((i) => i.summary)].join(" ").toLowerCase();
      return hay.includes(needle);
    });
  }, [res.data, filter, q]);

  if (res.error && !res.data) return <ErrorNote error={res.error} />;
  if (!res.data) return <Loading />;

  const all = res.data.work_orders;
  const open = all.filter((w) => w.status === "open");
  const counts = Object.fromEntries(FILTERS.map((f) => [f.id, all.filter(f.test).length])) as Record<Filter, number>;

  return (
    <>
      <div className="stats">
        <Stat label="Open" value={open.length} sub={`${counts.cancel} awaiting cancellation`} />
        <Stat label="Open estimate total" value={centsToUsd(open.reduce((n, w) => n + w.estimate.cents, 0))}
              sub={`${open.filter((w) => w.estimate.provisional).length} provisional until inspection`} />
        <Stat label="High urgency" value={counts.high} sub="safety or same-day" />
        <Stat label="Flagged for review" value={counts.review} sub="injection, language, assumptions" />
      </div>

      <section className="card">
        <div className="row" style={{ padding: "12px 14px", borderBottom: "1px solid var(--rule)", justifyContent: "space-between" }}>
          <div className="seg" role="group" aria-label="Filter work orders">
            {FILTERS.map((f) => (
              <button key={f.id} aria-pressed={filter === f.id} onClick={() => setFilter(f.id)}>
                {f.label} <span className="muted num">{counts[f.id]}</span>
              </button>
            ))}
          </div>
          <input className="input" style={{ maxWidth: 260 }} placeholder="Search customer, service, bike…" value={q}
                 onChange={(e) => setQ(e.target.value)} aria-label="Search work orders" />
        </div>
        {rows.length === 0 ? (
          <Empty>No work orders match.</Empty>
        ) : (
          <div className="tablebox">
            <table className="data">
              <thead>
                <tr>
                  <th>Customer</th><th>Services</th><th>Requested</th><th>Urgency</th><th>Flags</th><th className="right">Estimate</th>
                </tr>
              </thead>
              <tbody>
                {rows.map((w) => (
                  <tr key={w.id} className="click" tabIndex={0} onClick={() => ctx.openConversation(ctx.shopId, w.conversation_id)}
                      onKeyDown={(e) => e.key === "Enter" && ctx.openConversation(ctx.shopId, w.conversation_id)}>
                    <td>
                      <div className="cell-main">{w.customer_name ?? <span className="muted">Unsigned</span>}</div>
                      <div className="cell-sub">
                        {human(w.bike.type)}{w.bike.count > 1 ? ` × ${w.bike.count}` : ""}
                        {w.bike.brakes !== "unknown" ? ` · ${human(w.bike.brakes)}` : ""}
                      </div>
                    </td>
                    <td>
                      <div className="chips">
                        {w.lines.map((l, i) => (
                          <Chip key={i} code title={`${l.name} · ${l.unit_price} each`}>
                            {l.code}{l.quantity > 1 ? ` ×${l.quantity}` : ""}
                          </Chip>
                        ))}
                      </div>
                    </td>
                    <td>
                      {w.requested ? <span className="num">{w.requested.label}</span> : <span className="muted">Not given</span>}
                      {w.requested?.phrase ? <div className="cell-sub">“{w.requested.phrase}”</div> : null}
                    </td>
                    <td><Chip tone={urgencyTone(w.urgency)}>{w.urgency}</Chip></td>
                    <td>
                      <div className="chips">
                        {w.status === "cancel_requested" ? <Chip tone="bad">Cancel requested</Chip> : null}
                        {w.status === "completed" || w.status === "cancelled" ? <Chip>{w.status}</Chip> : null}
                        {w.review_reasons.map((r) => (
                          <Chip key={r} tone={r === "possible_prompt_injection" ? "bad" : "warn"}>{flagLabel(r)}</Chip>
                        ))}
                      </div>
                    </td>
                    <td className="right num">
                      <b>{w.estimate.formatted}</b>
                      {w.estimate.provisional ? <div className="cell-sub">provisional</div> : null}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>
    </>
  );
}
