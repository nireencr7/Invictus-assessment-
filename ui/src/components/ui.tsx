import type { ReactNode } from "react";
import { ratio, usd, type Tone } from "../lib/format";
import type { Spend, WorkOrder } from "../types";

export function Chip({ tone, children, title, code }: { tone?: Tone; children: ReactNode; title?: string; code?: boolean }) {
  const cls = ["chip", code ? "code" : "", tone && tone !== "neutral" ? tone : ""].filter(Boolean).join(" ");
  return (
    <span className={cls} title={title}>
      {children}
    </span>
  );
}

export function Stat({ label, value, sub }: { label: string; value: ReactNode; sub?: ReactNode }) {
  return (
    <div className="stat">
      <span className="label">{label}</span>
      <span className="value">{value}</span>
      {sub ? <span className="sub">{sub}</span> : null}
    </div>
  );
}

export function SpendMeter({ spend }: { spend: Spend }) {
  const spent = ratio(spend.spent_usd, spend.cap_usd);
  const reserved = Math.max(0, Math.min(1 - spent, ratio(spend.reserved_usd, spend.cap_usd)));
  return (
    <div className={`meter ${spend.state}`} role="meter" aria-valuenow={Number(spend.spent_usd)} aria-valuemax={Number(spend.cap_usd)}
         aria-label={`Spent ${usd(spend.spent_usd)} of ${usd(spend.cap_usd)}`}>
      <i className="spent" style={{ width: `${spent * 100}%` }} />
      <i className="reserved" style={{ width: `${reserved * 100}%` }} />
    </div>
  );
}

export function spendTone(state: Spend["state"]): Tone {
  return state === "exhausted" ? "bad" : state === "warning" ? "warn" : "ok";
}

export function urgencyTone(u: WorkOrder["urgency"]): Tone {
  return u === "high" ? "bad" : u === "low" ? "ok" : "neutral";
}

export function Loading() {
  return <div className="skeleton" aria-label="Loading" />;
}

export function ErrorNote({ error }: { error: string }) {
  return <div className="banner bad">{error}</div>;
}

export function Empty({ children }: { children: ReactNode }) {
  return <div className="empty">{children}</div>;
}

/** The work order as the shop sees it: lines priced from the catalog, quotes as evidence. */
export function Ticket({ order, shopName }: { order: WorkOrder; shopName: string }) {
  const evidence = new Map(order.issues.map((i) => [i.summary, i.evidence]));
  return (
    <div className="ticket">
      <div className="ticket-head">
        <span>WORK ORDER · {shopName}</span>
        <span>
          {order.status.replace("_", " ")} · urgency {order.urgency}
        </span>
      </div>
      <table>
        <tbody>
          {order.lines.map((l, i) => (
            <tr key={i}>
              <td>
                <Chip code>{l.code}</Chip> × {l.quantity}
                {l.wheel !== "not_applicable" ? <span className="muted"> ({l.wheel})</span> : null}
                <span className="ev-quote">{l.name}</span>
              </td>
              <td>{l.line_total}</td>
            </tr>
          ))}
        </tbody>
      </table>
      <div className="total">
        <span>Estimate{order.estimate.provisional ? " (provisional)" : ""}</span>
        <span>{order.estimate.formatted}</span>
      </div>
      {order.issues.length ? (
        <div style={{ marginTop: 10, display: "grid", gap: 4 }}>
          {order.issues.map((i) => (
            <div key={i.summary} style={{ fontSize: 13 }}>
              {i.summary}
              <span className="ev-quote">customer wrote: “{evidence.get(i.summary)}”</span>
            </div>
          ))}
        </div>
      ) : null}
    </div>
  );
}
