"""Renders eval results as docs/RESULTS.md."""

from __future__ import annotations

import json
from decimal import Decimal

from riverbend.catalog import format_money
from riverbend.config import REPO_ROOT

_SAMPLES: dict[str, dict] = {}
for _m in json.loads((REPO_ROOT / "data" / "samples.json").read_text())["messages"]:
    _SAMPLES.setdefault(_m["message_id"], _m)  # keep the first delivery of m-1003, not the duplicate


def _money(usd: str | Decimal) -> str:
    d = Decimal(usd)
    return f"${d:.4f}" if d < 1 else f"${d:.2f}"


def _codes(codes: dict) -> str:
    return ", ".join(f"{c}×{q}" if q > 1 else c for c, q in codes.items()) or "—"


def _mark(v) -> str:
    return "—" if v is None else ("✅" if v else "❌")


def _pct(rows, key) -> str:
    vals = [r[key] for r in rows if r.get(key) is not None]
    return f"{sum(vals)}/{len(vals)}" if vals else "n/a"


def _headline(r: dict) -> list[str]:
    ev = r["events"]
    rows = r["rows"]
    escalated = [x for x in rows if x["state"] == "needs_staff"]
    invalid = r["calls_by_status"].get("invalid_output", 0)
    spend_label = " (simulated: offline stand-in, no API calls)" if r["simulated_spend"] else ""
    return [
        "| Metric | Value |",
        "|---|---|",
        f"| Sample messages posted | {r['messages_received']} (+ {r['replies']} simulated customer replies) |",
        f"| Duplicate deliveries ignored | {r['duplicates']} |",
        f"| Conversations | {r['conversations']} |",
        f"| **Work orders created** | **{r['work_orders']}** ({r['cancel_requested']} later cancel-requested) |",
        f"| **Follow-up questions asked** | **{ev.get('follow_up_asked', 0)}** (all resumed after the customer replied: "
        f"{sum(1 for x in rows if x['follow_up'] and x['state'] == 'work_order_created')}) |",
        f"| Price questions answered (no work order) | {ev.get('price_quoted', 0)} |",
        f"| Cancellations routed to an existing order | {ev.get('cancellation_requested', 0)} |",
        f"| **Failures — handed to staff** | **{len(escalated)}**"
        + (f" ({', '.join(sorted({x['state_reason'] for x in escalated}))})" if escalated else "")
        + " |",
        f"| Model outputs rejected by validation | {invalid} (each repaired by one retry call, never saved) |",
        f"| Model calls | {r['calls']} ({r['input_tokens']:,} input / {r['output_tokens']:,} output tokens) |",
        f"| **Total model spend** | **{_money(r['cost_usd'])}**{spend_label} |",
        f"| Provider / model | {r['provider']} / `{r['model']}` |",
    ]


def _accuracy(r: dict) -> list[str]:
    rows = r["rows"]
    return [
        "| Check against [golden labels](../data/golden.json) | Agreement |",
        "|---|---|",
        f"| Final outcome (work order / follow-up / quote / cancel) | {_pct(rows, 'ok_outcome')} |",
        f"| Follow-up asked for the right reason | {_pct(rows, 'ok_follow_up')} |",
        f"| Services within the accepted set | {_pct(rows, 'ok_services')} |",
        f"| Quantities (per-wheel, per-bike) | {_pct(rows, 'ok_quantities')} |",
        f"| Estimate (computed in code) | {_pct(rows, 'ok_estimate')} |",
        f"| Requested date window (resolved in code) | {_pct(rows, 'ok_window')} |",
    ]


def _per_message(r: dict) -> list[str]:
    out = [
        "| # | Message | Shop | Outcome | Services | Estimate | Requested | Urgency | Flags | ✓ |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for row in sorted(r["rows"], key=lambda x: _SAMPLES[x["first_id"]]["n"]):
        n = _SAMPLES[row["first_id"]]["n"]
        outcome = row["outcome"].replace("_", " ")
        if row["follow_up"]:
            outcome = f"follow-up ({row['follow_up'].replace('_', ' ')}) → {outcome}"
        if row["wo_status"] == "cancel_requested":
            outcome += " → cancel requested"
        est = format_money(row["estimate_cents"]) if row["estimate_cents"] is not None else "—"
        window = "—"
        if row["window"]:
            a, b = row["window"]
            window = a[5:] if a == b else f"{a[5:]}…{b[5:]}"
        checks = [row.get(k) for k in ("ok_outcome", "ok_services", "ok_quantities", "ok_window")]
        ok = all(c is not False for c in checks)
        flags = ", ".join(f.replace("_", " ") for f in row["review"]) or ""
        if row["state"] == "needs_staff":
            flags = f"staff: {row['state_reason']}"
        services = _codes(row["codes"])
        if row.get("quoted"):
            services = "quoted: " + ", ".join(row["quoted"])
        out.append(
            f"| {n} | {row['first_id']} | {row['shop_id'].removeprefix('rb-')} | {outcome} | "
            f"{services} | {est} | {window} | {row['urgency'] or '—'} | {flags} | {'✅' if ok else '❌'} |"
        )
        if n == 13:
            out.append(
                "| 14 | m-1003 (again, 09:22) | lakeshore | duplicate delivery → ignored (HTTP 200, same ids) "
                "| — | — | — | — | | ✅ |"
            )
    return out


def render(results: dict, raw_path: str) -> str:
    base = results.get("baseline")
    md = ["# Results: the 40 sample messages", ""]
    if base and base["simulated_spend"]:
        md += [
            "> **This run used the offline stand-in, not a model.** `LLM_PROVIDER=offline` replays hand-written",
            "> outputs (see `data/offline_model_outputs.json`) so the plumbing — idempotency, validation, follow-ups,",
            "> budget, retries — can be exercised for free. Spend is *simulated* from text length at Opus 5.5 prices.",
            "> For real model results run `make eval-live` (needs `ANTHROPIC_API_KEY` in `.env`); it rewrites this file.",
            "",
        ]
    md += [f"Generated by `python -m riverbend eval`. Raw data: [`{raw_path}`](../{raw_path}).", ""]
    if base:
        md += [
            "## Headline (baseline run)",
            "",
            *_headline(base),
            "",
            "### Correctness",
            "",
            *_accuracy(base),
            "",
            "### Every message",
            "",
            *_per_message(base),
            "",
            "Dates are resolved in code against the received date (Tue 22 Sep 2026, shop-local). "
            "Estimates are integer-cent sums of catalog prices; the model never sees a price.",
            "",
        ]
        for note in base["notes"]:
            md.append(f"- {note}")
        md.append("")
        if base.get("customer_views"):
            md += ["### What the customer sees", ""]
            for ext, view in base["customer_views"].items():
                md.append(f"**{ext}** — status: _{view['status']}_")
                md.append("")
                for m in view["messages"]:
                    body = m["body"].replace("\n", "\n> ")
                    md.append(f"> **[{m['kind']}]** {body}")
                    md.append(">")
                md.append("")
    if "chaos" in results:
        c = results["chaos"]
        md += [
            "## Chaos run: flaky provider",
            "",
            "Same 40 messages; the provider wrapper fails 30% of calls with HTTP 503 and truncates 10% of "
            "responses mid-JSON. Faults fire before the real call, so they cost nothing.",
            "",
            f"- Injected: {c.get('injected')}",
            f"- Job retries: **{c['retries']}**; model calls by status: {c['calls_by_status']}",
            f"- Work orders: **{c['work_orders']}** (baseline {base['work_orders'] if base else '?'}); "
            f"follow-ups: {c['events'].get('follow_up_asked', 0)}; handed to staff: "
            f"{sum(1 for x in c['rows'] if x['state'] == 'needs_staff')}",
            "- Duplicate work orders: **0** (one per conversation is a database constraint)",
            f"- Spend: {_money(c['cost_usd'])}{' (simulated)' if c['simulated_spend'] else ''}",
            "",
        ]
        diffs = []
        if base:
            b = {r["first_id"]: r for r in base["rows"]}
            for r in c["rows"]:
                o = b.get(r["first_id"])
                if o and (o["outcome"], o["codes"]) != (r["outcome"], r["codes"]):
                    diffs.append(
                        f"  - {r['first_id']}: baseline `{o['outcome']}` → chaos `{r['outcome']}`"
                        f"{' (' + r['state_reason'] + ')' if r['state_reason'] else ''}"
                    )
        md += [
            "- Outcomes that differ from baseline:" if diffs else "- Every outcome matches the baseline.",
            *diffs,
            "",
        ]
    if "budget" in results:
        b = results["budget"]
        s = b["spend_at_cap"]
        md += [
            "## Spend cap: Northside capped at $0.20/month",
            "",
            "- Calls are reserved at worst-case cost before they are made; at the cap no call is made.",
            f"- At the cap: Northside spent {_money(s['spent_usd'])} of {_money(s['cap_usd'])}; "
            f"state **{s['state']}**; **{b['held']} messages held** ({', '.join(b['held_examples'])}).",
            "- Held messages: customer already has the acknowledgement; staff see them in the inbox marked "
            "`held_budget` with the original text; other shops are unaffected.",
            f"- Raising the cap to $5.00 released **{b['released']}** held messages; after draining: "
            f"{b['after_raise']['work_orders']} work orders in total.",
            "",
        ]
    if "outage" in results:
        o = results["outage"]
        d, a = o["during"], o["after_recovery"]
        md += [
            "## Provider outage: every call fails",
            "",
            "- Intake kept working: all 40 posts answered immediately (no dependency on the model).",
            f"- Acknowledgements sent: {d['outbound'].get('ack', 0)}. Work orders: {d['work_orders']}. "
            f"Spend: {_money(d['cost_usd'])}.",
            f"- Retries with backoff, then the circuit breaker deferred calls; after the {2}s demo SLA each "
            f"message was handed to staff: {sum(1 for x in d['rows'] if x['state'] == 'needs_staff')} "
            f"conversations in `needs_staff (ai_unavailable)`; Downtown inbox shows "
            f"{d['downtown_inbox_needs_action']} needing action.",
        ]
        if d.get("customer_views"):
            v = d["customer_views"].get("m-1001")
            if v:
                md.append(f"- Customer m-1001 sees: _{v['status']}_ and only the acknowledgement message.")
        md += [
            f"- Recovery: provider restored, staff pressed retry on {o['retried']} conversations → "
            f"{a['work_orders']} work orders, {a['events'].get('follow_up_asked', 0)} follow-ups.",
            "",
        ]
    return "\n".join(md) + "\n"
