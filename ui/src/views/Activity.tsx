import { Empty, ErrorNote, Loading } from "../components/ui";
import { centsToUsd, eventLabel, eventTone, reasonLabel, usd, when } from "../lib/format";
import { useResource } from "../lib/useResource";
import type { ActivityItem } from "../types";
import type { Ctx } from "./context";

/** One line of plain-language detail per event type; unknown types show nothing extra. */
export function describe(e: Pick<ActivityItem, "type" | "detail">): string {
  const d = e.detail as Record<string, unknown>;
  switch (e.type) {
    case "llm_call":
      return `${d.purpose === "repair" ? "Repair call" : "Extraction"} · ${d.valid ? "valid" : "rejected"} · ${usd(d.cost_usd as string)}${d.simulated ? " (simulated)" : ""}`;
    case "validation_failed":
      return ((d.errors as string[]) ?? []).slice(0, 2).join("; ");
    case "follow_up_asked":
      return `“${d.question}”`;
    case "work_order_created":
      return `${((d.codes as string[]) ?? []).join(", ")} · ${centsToUsd(d.estimate_cents as number)}`;
    case "escalated":
      return reasonLabel(d.reason as string);
    case "llm_transient_error":
      return String(d.error ?? "");
    case "price_quoted":
      return ((d.codes as string[]) ?? []).join(", ");
    case "budget_exhausted":
    case "budget_cap_changed":
      return `cap ${usd(d.cap_usd as string)}`;
    case "redirected":
      return `to ${d.to}`;
    default:
      return "";
  }
}

export function Activity({ ctx }: { ctx: Ctx }) {
  const res = useResource(() => ctx.source.activity(ctx.shopId), `activity:${ctx.shopId}`, ctx.poll);
  if (res.error && !res.data) return <ErrorNote error={res.error} />;
  if (!res.data) return <Loading />;
  if (!res.data.length) return <section className="card"><Empty>No activity yet.</Empty></section>;
  return (
    <section className="card">
      <div className="section-head" style={{ padding: "14px 16px 6px" }}>
        <h2>Timeline</h2>
        <span className="muted" style={{ fontSize: 13 }}>Newest first · every model call, rejection, question and hand-off</span>
      </div>
      <div className="feed">
        {res.data.map((e) => {
          const click = e.conversation_id ? () => ctx.openConversation(ctx.shopId, e.conversation_id!) : undefined;
          return (
            <div key={e.id} className={`ev${click ? " click" : ""}`} onClick={click} role={click ? "button" : undefined}
                 tabIndex={click ? 0 : undefined} onKeyDown={(k) => click && k.key === "Enter" && click()}>
              <time>{when(e.created_at)}</time>
              <span className={`dot ${eventTone(e.type, e.detail)}`} />
              <div className="what">
                {eventLabel(e.type)} {e.external_id ? <span className="mono muted">{e.external_id}</span> : null}
                {describe(e) ? <small>{describe(e)}</small> : null}
              </div>
            </div>
          );
        })}
      </div>
    </section>
  );
}
