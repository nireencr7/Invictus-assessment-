import { useState } from "react";
import { Chip, Empty, ErrorNote, Loading, SpendMeter, Stat, spendTone } from "../components/ui";
import { callTone, human, tokens, usd, when } from "../lib/format";
import { useResource } from "../lib/useResource";
import type { Ctx } from "./context";

export function Spend({ ctx }: { ctx: Ctx }) {
  const res = useResource(
    async () => {
      const [spend, calls] = await Promise.all([ctx.source.spend(ctx.shopId), ctx.source.llmCalls(ctx.shopId)]);
      return { spend, calls };
    },
    `spend:${ctx.shopId}`,
    ctx.poll,
  );
  const setCap = ctx.source.actions?.setCap;
  const [cap, setCapInput] = useState("");

  if (res.error && !res.data) return <ErrorNote error={res.error} />;
  if (!res.data) return <Loading />;
  const { spend, calls } = res.data;
  const settled = calls.filter((c) => c.cost_usd !== null);
  const avg = settled.length ? settled.reduce((n, c) => n + Number(c.cost_usd), 0) / settled.length : 0;
  const rejected = calls.filter((c) => c.status === "invalid_output").length;
  const failed = calls.filter((c) => !["ok", "invalid_output", "reserved"].includes(c.status)).length;

  return (
    <>
      <section className="card pad" style={{ display: "grid", gap: 12 }}>
        <div className="section-head">
          <h2>Monthly model budget · {spend.month.slice(0, 7)}</h2>
          <Chip tone={spendTone(spend.state)}>
            {spend.state === "exhausted" ? "Cap reached: new messages are held" : spend.state === "warning" ? "Over 80% of cap" : "Within cap"}
          </Chip>
        </div>
        <SpendMeter spend={spend} />
        <div className="row num" style={{ justifyContent: "space-between", fontSize: 13 }}>
          <span><b>{usd(spend.spent_usd)}</b> spent · {usd(spend.reserved_usd)} reserved for calls in flight</span>
          <span>cap <b>{usd(spend.cap_usd)}</b> · {usd(spend.remaining_usd)} left</span>
        </div>
        <p className="muted" style={{ margin: 0, fontSize: 13 }}>
          Each call reserves its worst-case cost before it's made and books the actual cost afterwards. At the cap no call is
          made: messages wait in the inbox, already acknowledged, until the cap is raised.
        </p>
        {setCap ? (
          <form className="row" onSubmit={async (e) => {
            e.preventDefault();
            if (!cap.trim()) return;
            try {
              const r = await setCap(ctx.shopId, cap.trim());
              ctx.toast(`Cap set to ${usd(cap)}${r.released ? `; released ${r.released} held message${r.released === 1 ? "" : "s"}` : ""}`);
              setCapInput("");
              res.reload();
            } catch (err) {
              ctx.toast(`Rejected: ${err instanceof Error ? err.message : String(err)}`);
            }
          }}>
            <label className="field" style={{ flex: "0 1 200px" }}>
              New monthly cap (USD)
              <input id="cap-input" className="input" inputMode="decimal" placeholder={spend.cap_usd} value={cap}
                     onChange={(e) => setCapInput(e.target.value)} />
            </label>
            <button className="btn" style={{ alignSelf: "flex-end" }}>Set cap</button>
          </form>
        ) : null}
      </section>

      <div className="stats">
        <Stat label="Model calls" value={spend.calls} sub={`${tokens(spend.input_tokens)} in · ${tokens(spend.output_tokens)} out`} />
        <Stat label="Average cost per call" value={usd(avg)} sub={settled[0]?.provider.startsWith("offline") ? "simulated" : settled[0]?.model ?? ""} />
        <Stat label="Outputs rejected" value={rejected} sub="failed validation; never saved" />
        <Stat label="Calls that failed" value={failed} sub="timeouts, outages, refusals" />
      </div>

      <section className="card">
        <div className="section-head" style={{ padding: "14px 16px 6px" }}>
          <h2>Model calls</h2>
          <span className="muted" style={{ fontSize: 13 }}>Newest first, including failed and rejected calls</span>
        </div>
        {calls.length === 0 ? <Empty>No calls yet.</Empty> : (
          <div className="tablebox">
            <table className="data">
              <thead><tr><th>When</th><th>Message</th><th>Call</th><th>Result</th><th className="right">Tokens in/out</th><th className="right">Cost</th><th className="right">Latency</th></tr></thead>
              <tbody>
                {calls.map((c) => (
                  <tr key={c.id} className="click" tabIndex={0} onClick={() => ctx.openConversation(ctx.shopId, c.conversation_id)}
                      onKeyDown={(e) => e.key === "Enter" && ctx.openConversation(ctx.shopId, c.conversation_id)}>
                    <td className="num" style={{ whiteSpace: "nowrap" }}>{when(c.created_at)}</td>
                    <td className="mono">{c.external_id}</td>
                    <td>{c.purpose === "repair" ? "repair" : "extract"}<div className="cell-sub">{c.model}</div></td>
                    <td>
                      <Chip tone={callTone(c.status)}>{human(c.status)}</Chip>
                      {c.error ? <div className="cell-sub" style={{ maxWidth: 340, overflowWrap: "anywhere" }}>{c.error}</div> : null}
                    </td>
                    <td className="right num">{tokens(c.input_tokens)} / {tokens(c.output_tokens)}</td>
                    <td className="right num">{usd(c.cost_usd)}</td>
                    <td className="right num">{c.latency_ms !== null ? `${c.latency_ms} ms` : "—"}</td>
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
