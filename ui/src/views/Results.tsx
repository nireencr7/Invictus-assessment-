import { Chip, Empty, ErrorNote, Loading, Stat } from "../components/ui";
import { centsToUsd, flagLabel, human, reasonLabel, usd } from "../lib/format";
import { useResource } from "../lib/useResource";
import type { EvalRow, EvalRun } from "../types";
import type { Ctx } from "./context";

const CHECKS: { key: keyof EvalRow; label: string }[] = [
  { key: "ok_outcome", label: "Final outcome (order / question / quote / cancel)" },
  { key: "ok_follow_up", label: "Follow-up asked for the right reason" },
  { key: "ok_services", label: "Services within the accepted set" },
  { key: "ok_quantities", label: "Quantities (per wheel, per bike)" },
  { key: "ok_estimate", label: "Estimate, computed in code" },
  { key: "ok_window", label: "Requested date window, resolved in code" },
];

function agreement(rows: EvalRow[], key: keyof EvalRow) {
  const vals = rows.map((r) => r[key]).filter((v) => v !== undefined && v !== null) as boolean[];
  return { ok: vals.filter(Boolean).length, n: vals.length };
}

function codes(c: Record<string, number>) {
  const e = Object.entries(c);
  return e.length ? e.map(([k, q]) => (q > 1 ? `${k}×${q}` : k)).join(", ") : "—";
}

export function Results({ ctx }: { ctx: Ctx }) {
  const res = useResource(() => ctx.source.evalSummary(), "results");
  if (res.error && !res.data) return <ErrorNote error={res.error} />;
  if (res.loading && !res.data) return <Loading />;
  const ev = res.data;
  if (!ev || !ev.baseline) return <section className="card"><Empty>No eval results bundled with this build.</Empty></section>;
  const b: EvalRun = ev.baseline;
  const staff = b.rows.filter((r) => r.state === "needs_staff").length;

  return (
    <>
      {b.simulated_spend ? (
        <div className="banner">
          <b>Offline stand-in run.</b> These numbers come from hand-written model outputs (three deliberately broken) so the
          pipeline can be exercised for free. They test the system, not the model. <code>make eval-live</code> reruns it against
          Claude with real spend. Source: <code>results/{ev.source}</code>.
        </div>
      ) : null}

      <div className="stats">
        <Stat label="Sample messages" value={b.messages_received} sub={`+ ${b.replies} simulated replies · ${b.duplicates} duplicate`} />
        <Stat label="Work orders" value={b.work_orders} sub={`${b.cancel_requested} later cancel-requested`} />
        <Stat label="Follow-ups asked" value={b.events.follow_up_asked ?? 0} sub="all resumed after the reply" />
        <Stat label="Handed to staff" value={staff} sub={`${b.calls_by_status.invalid_output ?? 0} bad outputs rejected and repaired`} />
        <Stat label="Model spend" value={usd(b.cost_usd)} sub={`${b.calls} calls${b.simulated_spend ? " · simulated" : ""}`} />
      </div>

      <section className="card pad" style={{ display: "grid", gap: 8 }}>
        <h2 style={{ fontSize: 17 }}>Agreement with golden labels</h2>
        {CHECKS.map((c) => {
          const a = agreement(b.rows, c.key);
          return (
            <div key={c.key} className="row" style={{ justifyContent: "space-between", borderBottom: "1px solid var(--rule)", paddingBottom: 6 }}>
              <span>{c.label}</span>
              <Chip tone={a.ok === a.n ? "ok" : "warn"}><span className="num">{a.ok}/{a.n}</span></Chip>
            </div>
          );
        })}
      </section>

      <div className="shops">
        {ev.chaos ? (
          <section className="card pad">
            <h3>Flaky provider</h3>
            <p className="muted" style={{ fontSize: 13.5 }}>30% of calls fail with HTTP 503; 10% return truncated JSON.</p>
            <p style={{ fontSize: 14 }}>
              <b>{ev.chaos.retries}</b> retries, <b>{ev.chaos.work_orders}</b> work orders (baseline {b.work_orders}),
              <b> 0</b> duplicates; spend {usd(ev.chaos.cost_usd)}.
            </p>
          </section>
        ) : null}
        {ev.budget ? (
          <section className="card pad">
            <h3>Spend cap at $0.20</h3>
            <p className="muted" style={{ fontSize: 13.5 }}>Northside capped mid-run.</p>
            <p style={{ fontSize: 14 }}>
              <b>{ev.budget.held}</b> messages held, not lost; raising the cap released <b>{ev.budget.released}</b>;
              {" "}{ev.budget.workOrdersAfterRaise} work orders in the end.
            </p>
          </section>
        ) : null}
        {ev.outage ? (
          <section className="card pad">
            <h3>Provider down</h3>
            <p className="muted" style={{ fontSize: 13.5 }}>Every model call fails.</p>
            <p style={{ fontSize: 14 }}>
              <b>{ev.outage.acks}</b> instant acknowledgements, {usd(ev.outage.costDuring)} spent, <b>{ev.outage.handedToStaff}</b> handed
              to staff; after recovery, {ev.outage.workOrdersAfterRecovery} work orders.
            </p>
          </section>
        ) : null}
      </div>

      <section className="card">
        <div className="section-head" style={{ padding: "14px 16px 6px" }}>
          <h2>Every conversation</h2>
          <span className="muted" style={{ fontSize: 13 }}>Prices and dates are computed by code, not the model</span>
        </div>
        <div className="tablebox">
          <table className="data">
            <thead><tr><th>Message</th><th>Shop</th><th>Outcome</th><th>Services</th><th className="right">Estimate</th><th>Window</th><th>Flags</th><th>Golden</th></tr></thead>
            <tbody>
              {[...b.rows].sort((x, y) => x.first_id.localeCompare(y.first_id)).map((r) => {
                const ok = [r.ok_outcome, r.ok_services, r.ok_quantities, r.ok_window].every((v) => v !== false);
                return (
                  <tr key={r.first_id}>
                    <td className="mono">{r.first_id}</td>
                    <td>{ctx.shopName(r.shop_id).replace("Riverbend Cycles ", "")}</td>
                    <td>
                      {r.follow_up ? <div className="cell-sub">asked: {reasonLabel(r.follow_up)}</div> : null}
                      {human(r.outcome)}{r.wo_status === "cancel_requested" ? " → cancel requested" : ""}
                    </td>
                    <td className="mono" style={{ fontSize: 12 }}>{r.quoted ? `quoted: ${r.quoted.join(", ")}` : codes(r.codes)}</td>
                    <td className="right num">{centsToUsd(r.estimate_cents)}</td>
                    <td className="num" style={{ whiteSpace: "nowrap" }}>
                      {r.window ? (r.window[0] === r.window[1] ? r.window[0].slice(5) : `${r.window[0].slice(5)}…${r.window[1].slice(5)}`) : "—"}
                    </td>
                    <td><div className="chips">{r.review.map((f) => <Chip key={f} tone="warn">{flagLabel(f)}</Chip>)}</div></td>
                    <td><Chip tone={ok ? "ok" : "bad"}>{ok ? "match" : "differs"}</Chip></td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      </section>
    </>
  );
}
