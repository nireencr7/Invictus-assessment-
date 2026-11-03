import { useState, type ReactNode } from "react";
import { Chip, Empty, ErrorNote, Loading } from "../components/ui";
import type { DevTools } from "../data/source";
import { callTone, centsToUsd, eventTone, human, jobTone, messageKindLabel, outcomeTone, tokens, usd, when } from "../lib/format";
import { refreshAll, useResource } from "../lib/useResource";
import type { DevJob, DevRun } from "../types";
import type { Ctx } from "./context";

/**
 * Dev only: the job queue, a button that runs one turn of the worker, what that run did, and the
 * server log. Nothing here refreshes on its own: press Run, or Refresh in the top bar.
 */
export function Worker({ ctx }: { ctx: Ctx }) {
  const tools = useResource(() => ctx.source.devTools(), "dev-tools");
  if (tools.data === undefined) return tools.error ? <ErrorNote error={tools.error} /> : <Loading />;
  if (tools.data === null) {
    return (
      <section className="card">
        <Empty>
          Only in local development (<code>make dev</code> or <code>make dev-step</code>). On GCP the worker is its
          own Cloud Run service and its logs are in Cloud Logging.
        </Empty>
      </section>
    );
  }
  return <WorkerTools ctx={ctx} tools={tools.data} />;
}

function WorkerTools({ ctx, tools }: { ctx: Ctx; tools: DevTools }) {
  const [scope, setScope] = useState<"open" | "all">("open");
  const status = useResource(() => tools.status(), "dev-status");
  const jobs = useResource(() => tools.jobs(scope), `dev-jobs:${scope}`);
  const logs = useResource(() => tools.logs(), "dev-logs");
  const [run, setRun] = useState<DevRun | null>(null);
  const [busy, setBusy] = useState(false);
  const stepMode = status.data ? !status.data.embeddedWorker : null;

  const runOnce = async () => {
    setBusy(true);
    try {
      const r = await tools.runOnce();
      setRun(r);
      ctx.toast(r.ran ? `Job ${r.job.id} → ${human(r.outcome)}` : "No job is ready");
    } catch (e) {
      ctx.toast(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
      void refreshAll(); // the queue, the log and the inbox count all changed
    }
  };

  return (
    <>
      <section className="card pad" style={{ display: "flex", flexDirection: "column", gap: 10 }}>
        <div className="row" style={{ justifyContent: "space-between" }}>
          <div className="row">
            {stepMode === null ? null : stepMode ? (
              <Chip tone="info">Background worker off · step mode</Chip>
            ) : (
              <Chip tone="ok">
                Background worker on · {status.data!.workerThreads} thread{status.data!.workerThreads === 1 ? "" : "s"}
              </Chip>
            )}
            {status.data ? <Chip code>{status.data.provider} · {status.data.model}</Chip> : null}
            {status.data ? (
              <Chip tone={status.data.breaker === "closed" ? "ok" : "bad"}>breaker {status.data.breaker}</Chip>
            ) : null}
          </div>
          <button className="btn primary" disabled={busy} onClick={() => void runOnce()}>
            {busy ? "Running… (a model call takes a few seconds)" : "Run the worker once"}
          </button>
        </div>
        <span className="muted" style={{ fontSize: 13 }}>
          {stepMode
            ? "make dev-step: nothing is processed until you press Run. Each press claims the next job (shops take turns: fewest jobs running, then the shop served longest ago, oldest job first within a shop) and runs it through the whole pipeline."
            : "make dev: the background worker takes each job within half a second, so the queue is usually empty. It runs 1 thread, 2 above 25 waiting jobs and 3 above 50, and each free thread serves the shop with the fewest jobs running, then the shop served longest ago. Start the API with make dev-step to run jobs one at a time here."}
        </span>
      </section>

      {run ? <LastRun ctx={ctx} run={run} /> : null}

      <section className="card">
        <div className="section-head" style={{ padding: "14px 16px 6px" }}>
          <h2>Job queue</h2>
          <div className="seg" role="group" aria-label="Which jobs">
            <button aria-pressed={scope === "open"} onClick={() => setScope("open")}>Waiting and running</button>
            <button aria-pressed={scope === "all"} onClick={() => setScope("all")}>All, newest first</button>
          </div>
        </div>
        {jobs.error && !jobs.data ? <ErrorNote error={jobs.error} /> : null}
        {!jobs.data ? (
          jobs.error ? null : <Loading />
        ) : jobs.data.length === 0 ? (
          <Empty>No jobs {scope === "open" ? "waiting. Send a message, then come back here." : "yet."}</Empty>
        ) : (
          <JobTable ctx={ctx} jobs={jobs.data} />
        )}
      </section>

      <section className="card">
        <div className="section-head" style={{ padding: "14px 16px 6px" }}>
          <h2>Server log</h2>
          <span className="muted" style={{ fontSize: 13 }}>
            Application lines only, oldest first · uvicorn's request lines stay in the terminal
          </span>
        </div>
        {logs.error && !logs.data ? <ErrorNote error={logs.error} /> : null}
        {logs.data && logs.data.length === 0 ? <Empty>Nothing logged yet.</Empty> : null}
        {logs.data?.length ? (
          <div className="log">
            {logs.data.map((l) => (
              <div key={l.seq} className={`log-line ${l.level === "ERROR" || l.level === "CRITICAL" ? "bad" : l.level === "WARNING" ? "warn" : ""}`}>
                <time>{l.time.slice(11, 19)}</time> <b>{l.level}</b> <span className="muted">{l.logger}</span> {l.message}
              </div>
            ))}
          </div>
        ) : null}
      </section>
    </>
  );
}

function JobTable({ ctx, jobs }: { ctx: Ctx; jobs: DevJob[] }) {
  return (
    <div className="tablebox">
      <table className="data">
        <thead>
          <tr>
            <th>Job</th><th>Shop</th><th>Status</th><th>Attempts</th><th>Run after</th><th>Message</th><th>Last error</th>
          </tr>
        </thead>
        <tbody>
          {jobs.map((j) => {
            const open = j.message ? () => ctx.openConversation(j.shop_id, j.message!.conversation_id) : undefined;
            return (
              <tr key={j.id} className={open ? "click" : undefined} onClick={open}>
                <td className="mono">#{j.id}</td>
                <td>{ctx.shopName(j.shop_id).replace("Riverbend Cycles ", "")}</td>
                <td>
                  <div className="chips">
                    <Chip tone={jobTone(j.status)}>{j.status}</Chip>
                    {j.ready ? <Chip tone="ok">ready</Chip> : null}
                    {j.park_reason ? <Chip tone="warn">{human(j.park_reason)}</Chip> : null}
                  </div>
                </td>
                <td className="num">{j.attempts}</td>
                <td className="num">{when(j.run_after)}</td>
                <td>
                  {j.message ? (
                    <>
                      <div className="cell-main">{j.message.body}</div>
                      <div className="cell-sub mono">{j.message.external_id} · message {human(j.message.status)}</div>
                    </>
                  ) : null}
                </td>
                <td className="cell-sub">{j.last_error ?? ""}</td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

function LastRun({ ctx, run }: { ctx: Ctx; run: DevRun }) {
  if (!run.ran) {
    return (
      <section className="card pad">
        <div className="section-head"><h2>Last run</h2><Chip>no job was ready</Chip></div>
        {run.queued_for_later.length ? (
          <p className="muted" style={{ fontSize: 13.5 }}>
            Waiting for a retry time:{" "}
            {run.queued_for_later.map((q) => `#${q.id} after ${when(q.run_after)} (attempt ${q.attempts})`).join(" · ")}
          </p>
        ) : (
          <p className="muted" style={{ fontSize: 13.5 }}>The queue is empty.</p>
        )}
      </section>
    );
  }
  const conv = run.conversation;
  return (
    <section className="card pad" style={{ display: "flex", flexDirection: "column", gap: 14 }}>
      <div className="section-head">
        <h2>
          Last run · job #{run.job.id} · {ctx.shopName(run.job.shop_id).replace("Riverbend Cycles ", "")} · attempt {run.job.attempt}
        </h2>
        <Chip tone={outcomeTone(run.outcome)}>{human(run.outcome)}</Chip>
      </div>
      {run.message ? (
        <div>
          <span className="mono muted">{run.message.external_id}</span> {run.message.body}
        </div>
      ) : null}

      <Step title="1 · Model calls" hint="reserve the worst case → call → book the real cost">
        {run.model_calls?.length ? (
          <div className="tablebox">
            <table className="data">
              <thead>
                <tr><th>Purpose</th><th>Status</th><th className="right">Reserved</th><th className="right">Cost</th><th className="right">Tokens in / out</th><th className="right">Latency</th></tr>
              </thead>
              <tbody>
                {run.model_calls.map((c, i) => (
                  <tr key={i}>
                    <td>{c.purpose}</td>
                    <td><Chip tone={callTone(c.status)}>{human(c.status)}</Chip>{c.error ? <div className="cell-sub">{c.error}</div> : null}</td>
                    <td className="right num">{usd(c.reserved_usd)}</td>
                    <td className="right num">{c.cost_usd === null ? "—" : usd(c.cost_usd)}</td>
                    <td className="right num">{tokens(c.input_tokens)} / {tokens(c.output_tokens)}</td>
                    <td className="right num">{c.latency_ms === null ? "—" : `${(c.latency_ms / 1000).toFixed(1)} s`}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : (
          <span className="muted">No model call in this run.</span>
        )}
      </Step>

      {run.model_output && run.code_decided ? (
        <div className="split">
          <Step title="2 · What the model said" hint="catalog codes and the customer's own words; no prices">
            <pre className="json">{JSON.stringify(run.model_output, null, 2)}</pre>
          </Step>
          <Step title="3 · What code decided" hint="prices from the catalog, real dates">
            <table className="data">
              <thead><tr><th>Service</th><th>Wheel</th><th className="right">Qty</th><th className="right">Unit</th><th className="right">Total</th></tr></thead>
              <tbody>
                {run.code_decided.lines.map((l) => (
                  <tr key={`${l.service_code}-${l.wheel}`}>
                    <td className="mono">{l.service_code}</td>
                    <td>{human(l.wheel)}</td>
                    <td className="right num">{l.quantity}</td>
                    <td className="right num">{centsToUsd(l.unit_price_cents)}</td>
                    <td className="right num">{centsToUsd(l.line_total_cents)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
            <div style={{ fontSize: 13.5, marginTop: 8 }}>
              Estimate <b>{centsToUsd(run.code_decided.estimate_cents)}</b> · urgency {run.code_decided.urgency} · requested{" "}
              {run.code_decided.requested_earliest ?? "no date"}
              {run.code_decided.requested_latest && run.code_decided.requested_latest !== run.code_decided.requested_earliest
                ? ` to ${run.code_decided.requested_latest}`
                : ""}
              {run.code_decided.requested_time_of_day ? `, ${run.code_decided.requested_time_of_day}` : ""}
              {run.code_decided.review_reasons.length ? ` · for staff: ${run.code_decided.review_reasons.map(human).join(", ")}` : ""}
            </div>
          </Step>
        </div>
      ) : null}

      <Step title="Events written" hint="the conversation's timeline">
        {run.events?.length ? (
          <div className="feed">
            {run.events.map((e, i) => (
              <div key={i} className="ev">
                <time>{e.type === "llm_call" ? "model" : "event"}</time>
                <span className={`dot ${eventTone(e.type, e.detail)}`} />
                <div className="what">
                  {human(e.type)}
                  {Object.keys(e.detail).length ? <small className="mono">{JSON.stringify(e.detail)}</small> : null}
                </div>
              </div>
            ))}
          </div>
        ) : (
          <span className="muted">None.</span>
        )}
      </Step>

      <Step title="Sent to the customer">
        {run.messages_sent?.length ? (
          run.messages_sent.map((m, i) => (
            <div key={i} className="sent">
              <Chip>{messageKindLabel(m.kind)}</Chip>
              <div style={{ whiteSpace: "pre-wrap" }}>{m.body}</div>
            </div>
          ))
        ) : (
          <span className="muted">Nothing.</span>
        )}
      </Step>

      <Step title="Afterwards">
        <div className="row" style={{ fontSize: 13.5 }}>
          {conv ? (
            <>
              Conversation <Chip tone="info">{human(conv.state)}</Chip>
              {conv.state_reason ? <span className="muted">({human(conv.state_reason)})</span> : null}
              <span className="muted">· follow-ups asked {conv.follow_ups_asked}</span>
            </>
          ) : null}
          <span>· Job <Chip tone={jobTone(run.job_after.status)}>{run.job_after.status}</Chip></span>
          {run.job_after.status === "queued" ? <span className="muted">retry after {when(run.job_after.run_after)}</span> : null}
          {run.job_after.last_error ? <span className="muted">· {run.job_after.last_error}</span> : null}
          {conv ? (
            <button className="btn" onClick={() => ctx.openConversation(run.job.shop_id, conv.id)}>Open conversation</button>
          ) : null}
        </div>
      </Step>
    </section>
  );
}

function Step({ title, hint, children }: { title: string; hint?: string; children: ReactNode }) {
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 6, minWidth: 0 }}>
      <h4 className="step-title">
        {title} {hint ? <span className="muted">· {hint}</span> : null}
      </h4>
      {children}
    </div>
  );
}
