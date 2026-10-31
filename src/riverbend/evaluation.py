"""Runs the 40 brief samples through the real HTTP API and worker, and writes docs/RESULTS.md.

Scenarios (each in its own fresh database):
  baseline  the samples as given, plus simulated customer replies to any follow-up question
  chaos     same, with 30% of provider calls failing and 10% returning truncated JSON
  budget    Northside's monthly cap set to $0.20: what happens at the cap, then after raising it
  outage    the provider is down for the whole run: what customers and staff see; then recovery

    uv run python -m riverbend eval                      # offline stand-in, $0
    LLM_PROVIDER=anthropic uv run python -m riverbend eval --scenarios baseline   # live
"""

from __future__ import annotations

import json
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import psycopg
from fastapi.testclient import TestClient
from psycopg.rows import dict_row

from riverbend.api import create_app
from riverbend.catalog import Catalog, format_money
from riverbend.cli import seed
from riverbend.config import REPO_ROOT, Settings
from riverbend.db import run_migrations
from riverbend.services import Services, build_services

SAMPLES = REPO_ROOT / "data" / "samples.json"
GOLDEN = REPO_ROOT / "data" / "golden.json"
REPLIES = REPO_ROOT / "data" / "simulated_replies.json"

SCENARIOS = {
    "baseline": {},
    "chaos": {
        "llm_faults": "flaky:0.3,garbage:0.1",
        "breaker_failure_threshold": 4,
        "breaker_cooldown_seconds": 0.3,
    },
    "budget": {},
    "outage": {
        "llm_faults": "outage",
        "max_attempts": 3,
        "processing_sla_seconds": 2.0,
        "breaker_failure_threshold": 3,
        "breaker_cooldown_seconds": 0.2,
    },
}
FAST = {"retry_base_seconds": 0.05, "retry_max_seconds": 0.3, "worker_poll_seconds": 0.01}


def _server_and_db(dsn: str) -> tuple[str, str]:
    base, _, name = dsn.rpartition("/")
    return base, name.split("?")[0]


def _fresh_database(settings: Settings, name: str) -> tuple[str, str]:
    admin_base, _ = _server_and_db(settings.database_admin_url.get_secret_value())
    app_base, _ = _server_and_db(settings.database_url.get_secret_value())
    with psycopg.connect(f"{admin_base}/postgres", autocommit=True) as conn:
        conn.execute(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)")
        conn.execute(f"CREATE DATABASE {name}")
    admin, app = f"{admin_base}/{name}", f"{app_base}/{name}"
    run_migrations(admin, settings.app_db_password.get_secret_value())
    seed(admin, Catalog.load(settings.catalog_path), settings.default_monthly_cap_usd, settings.shop_timezone)
    return admin, app


@dataclass
class Scenario:
    name: str
    settings: Settings
    admin_dsn: str
    services: Services
    client: TestClient
    notes: list[str] = field(default_factory=list)
    started: float = field(default_factory=time.monotonic)

    def admin(self):
        return psycopg.connect(self.admin_dsn, autocommit=True, row_factory=dict_row)


def _headers(settings: Settings, shop_id: str | None = None, admin: bool = False) -> dict:
    if admin:
        return {"Authorization": f"Bearer {settings.admin_token.get_secret_value()}"}
    if shop_id:
        token = {v: k for k, v in settings.staff_token_map().items()}[shop_id]
        return {"Authorization": f"Bearer {token}"}
    return {"X-Ingest-Key": next(iter(settings.ingest_key_map()))}


def _post_samples(sc: Scenario) -> list[dict]:
    data = json.loads(SAMPLES.read_text())
    responses = []
    for m in data["messages"]:
        r = sc.client.post(
            f"/v1/shops/{m['shop_id']}/inbound-messages",
            headers=_headers(sc.settings),
            json={
                "message_id": m["message_id"],
                "channel": m["channel"],
                "received_at": f"{data['date']}T{m['received']}:00",
                "body": m["body"],
            },
        )
        responses.append({"n": m["n"], "message_id": m["message_id"], "status": r.status_code, **r.json()})
    return responses


def _send_replies(sc: Scenario) -> list[str]:
    replies = json.loads(REPLIES.read_text())["replies"]
    sent = []
    with sc.admin() as conn:
        waiting = conn.execute(
            """SELECT c.shop_id, c.reply_token, c.channel, m.external_id, m.received_at
               FROM conversations c JOIN inbound_messages m ON m.conversation_id = c.id
               WHERE c.state = 'awaiting_customer'
                 AND m.created_at = (SELECT min(created_at) FROM inbound_messages WHERE conversation_id = c.id)"""
        ).fetchall()
    for w in waiting:
        text = replies.get(w["external_id"])
        if not text:
            continue
        received = w["received_at"] + timedelta(minutes=30)
        r = sc.client.post(
            f"/v1/shops/{w['shop_id']}/inbound-messages",
            headers=_headers(sc.settings),
            json={
                "message_id": f"{w['external_id']}-reply",
                "channel": w["channel"],
                "received_at": received.isoformat(),
                "body": text,
                "reply_token": w["reply_token"],
            },
        )
        assert r.status_code == 202, r.text
        sent.append(w["external_id"])
    return sent


def _start(settings: Settings, name: str, overrides: dict) -> Scenario:
    admin, app = _fresh_database(settings, f"riverbend_eval_{name}")
    s = settings.model_copy(
        update={
            "database_url": type(settings.database_url)(app),
            "database_admin_url": type(settings.database_admin_url)(admin),
            **FAST,
            **overrides,
        }
    )
    services = build_services(s)
    client = TestClient(create_app(s, services))
    client.__enter__()
    return Scenario(name, s, admin, services, client)


def _stop(sc: Scenario) -> None:
    sc.client.__exit__(None, None, None)
    sc.services.close()


# ---------------------------------------------------------------------- collection & scoring
def _collect(sc: Scenario) -> dict:
    golden = json.loads(GOLDEN.read_text())["expected"]
    with sc.admin() as conn:
        convs = conn.execute(
            """SELECT c.*, (SELECT external_id FROM inbound_messages WHERE conversation_id = c.id
                            ORDER BY created_at LIMIT 1) AS first_id,
                      (SELECT count(*) FROM inbound_messages WHERE conversation_id = c.id) AS turns
               FROM conversations c"""
        ).fetchall()
        wos = {r["conversation_id"]: r for r in conn.execute("SELECT * FROM work_orders").fetchall()}
        lines: dict = {}
        for line in conn.execute("SELECT * FROM work_order_lines ORDER BY line_no").fetchall():
            lines.setdefault(line["work_order_id"], []).append(line)
        calls = conn.execute("SELECT * FROM llm_calls").fetchall()
        msgs = conn.execute("SELECT * FROM inbound_messages").fetchall()
        followups = {
            r["conversation_id"]: r
            for r in conn.execute(
                "SELECT conversation_id, detail FROM events WHERE type = 'follow_up_asked'"
            ).fetchall()
        }
        events = Counter(r["type"] for r in conn.execute("SELECT type FROM events").fetchall())
        jobs = conn.execute("SELECT status, attempts FROM jobs").fetchall()
        outbound = Counter(r["kind"] for r in conn.execute("SELECT kind FROM outbound_messages").fetchall())
        spend = conn.execute(
            "SELECT shop_id, cap_usd, spent_usd, reserved_usd, exhausted_at FROM shop_spend"
        ).fetchall()
        dup = conn.execute(
            "SELECT coalesce(sum(duplicate_deliveries), 0) AS n FROM inbound_messages"
        ).fetchone()["n"]
        quoted = {
            r["conversation_id"]: r["detail"]["codes"]
            for r in conn.execute(
                "SELECT conversation_id, detail FROM events WHERE type = 'price_quoted'"
            ).fetchall()
        }

    msg_by_conv: dict = {}
    for m in msgs:
        msg_by_conv.setdefault(m["conversation_id"], []).append(m)
    calls_by_msg = Counter(c["message_id"] for c in calls)
    rows = []
    for c in sorted(convs, key=lambda c: c["first_id"]):
        wo = wos.get(c["id"])
        wo_lines = lines.get(wo["id"], []) if wo else []
        codes = Counter()
        for line in wo_lines:
            codes[line["service_code"]] += line["quantity"]
        final_key = f"{c['first_id']}#{c['turns']}"
        g = golden.get(final_key) or golden.get(f"{c['first_id']}#1") or {}
        g1 = golden.get(f"{c['first_id']}#1", {})
        last_msg = sorted(msg_by_conv[c["id"]], key=lambda m: m["created_at"])[-1]
        outcome = last_msg["outcome"] or last_msg["status"]
        fu = followups.get(c["id"])
        row = {
            "first_id": c["first_id"],
            "shop_id": c["shop_id"],
            "state": c["state"],
            "outcome": outcome,
            "follow_up": fu["detail"]["reason"] if fu else None,
            "codes": dict(codes),
            "estimate_cents": wo["estimate_cents"] if wo else None,
            "window": [wo["requested_earliest"].isoformat(), wo["requested_latest"].isoformat()]
            if wo and wo["requested_earliest"]
            else None,
            "urgency": wo["urgency"] if wo else None,
            "review": list(wo["review_reasons"]) if wo else [],
            "wo_status": wo["status"] if wo else None,
            "calls": sum(calls_by_msg[m["id"]] for m in msg_by_conv[c["id"]]),
            "state_reason": c["state_reason"],
            "quoted": quoted.get(c["id"]),
        }
        # Scoring against golden labels.
        expected_outcome = g.get("outcome")
        row["golden_outcome"] = expected_outcome
        row["ok_outcome"] = outcome == expected_outcome or (
            expected_outcome == "work_order_created" and outcome == "work_order_created"
        )
        if g1.get("outcome") == "follow_up_asked":
            row["ok_follow_up"] = row["follow_up"] == g1.get("follow_up_reason")
        if g.get("codes") and expected_outcome == "work_order_created":
            accept = [sorted(a) for a in g.get("accept", [])] or [sorted(g["codes"])]
            row["ok_services"] = sorted(codes) in accept
            row["ok_quantities"] = dict(codes) == g["codes"] if sorted(codes) == sorted(g["codes"]) else None
            row["ok_estimate"] = (
                row["estimate_cents"] == g.get("estimate_cents") if row["ok_quantities"] else None
            )
            row["ok_window"] = row["window"] == g.get("window") if g.get("window") else None
            row["golden_codes"] = g["codes"]
        rows.append(row)

    total_cost = sum((c["cost_usd"] or Decimal(0)) for c in calls)
    simulated = sc.services.provider.name.startswith("offline")
    return {
        "scenario": sc.name,
        "provider": sc.services.provider.name,
        "model": sc.services.provider.model,
        "simulated_spend": simulated,
        "rows": rows,
        "events": dict(events),
        "outbound": dict(outbound),
        "messages_received": sum(1 for m in msgs if not m["external_id"].endswith("-reply")) + int(dup),
        "replies": sum(1 for m in msgs if m["external_id"].endswith("-reply")),
        "duplicates": int(dup),
        "conversations": len(convs),
        "work_orders": len(wos),
        "work_orders_open": sum(1 for w in wos.values() if w["status"] == "open"),
        "cancel_requested": sum(1 for w in wos.values() if w["status"] == "cancel_requested"),
        "calls": len(calls),
        "calls_by_status": dict(Counter(c["status"] for c in calls)),
        "input_tokens": sum(c["input_tokens"] or 0 for c in calls),
        "output_tokens": sum(c["output_tokens"] or 0 for c in calls),
        "cost_usd": str(total_cost),
        "jobs": dict(Counter(j["status"] for j in jobs)),
        "retries": sum(max(j["attempts"] - 1, 0) for j in jobs),
        "spend": [{k: (str(v) if isinstance(v, Decimal) else v) for k, v in s.items()} for s in spend],
        "notes": sc.notes,
        "seconds": round(time.monotonic() - sc.started, 1),
    }


# ---------------------------------------------------------------------- scenarios
def _run_samples(sc: Scenario, *, replies: bool = True) -> None:
    posted = _post_samples(sc)
    statuses = Counter(p["status"] for p in posted)
    sc.notes.append(f"HTTP responses to the 40 posts: {dict(sorted(statuses.items()))}")
    worker = sc.services.worker()
    worker.drain(max_wait_seconds=900)
    if replies:
        sent = _send_replies(sc)
        if sent:
            sc.notes.append(f"Simulated customer replies sent to: {', '.join(sent)}")
            worker.drain(max_wait_seconds=600)


def scenario_baseline(settings: Settings) -> dict:
    sc = _start(settings, "baseline", SCENARIOS["baseline"])
    try:
        _run_samples(sc)
        result = _collect(sc)
        result["customer_views"] = _customer_views(sc, ["m-1001", "m-1027", "m-1018", "m-1009"])
        return result
    finally:
        _stop(sc)


def scenario_chaos(settings: Settings) -> dict:
    sc = _start(settings, "chaos", SCENARIOS["chaos"])
    try:
        _run_samples(sc)
        result = _collect(sc)
        result["injected"] = getattr(sc.services.provider, "injected", {})
        return result
    finally:
        _stop(sc)


def scenario_budget(settings: Settings) -> dict:
    sc = _start(settings, "budget", SCENARIOS["budget"])
    try:
        r = sc.client.put(
            "/v1/admin/shops/rb-northside/budget",
            headers=_headers(sc.settings, admin=True),
            json={"monthly_cap_usd": "0.20"},
        )
        assert r.status_code == 200, r.text
        _run_samples(sc, replies=False)
        at_cap = _collect(sc)
        spend = sc.client.get(
            "/v1/shops/rb-northside/spend", headers=_headers(sc.settings, "rb-northside")
        ).json()
        inbox = sc.client.get(
            "/v1/shops/rb-northside/inbox", headers=_headers(sc.settings, "rb-northside")
        ).json()
        held = [i for i in inbox["items"] if i["message_status"] == "held_budget"]
        r = sc.client.put(
            "/v1/admin/shops/rb-northside/budget",
            headers=_headers(sc.settings, admin=True),
            json={"monthly_cap_usd": "5.00"},
        )
        released = r.json()["released_held_messages"]
        sc.services.worker().drain(max_wait_seconds=600)
        _send_replies(sc)
        sc.services.worker().drain(max_wait_seconds=600)
        after = _collect(sc)
        return {
            "at_cap": at_cap,
            "after_raise": after,
            "spend_at_cap": spend,
            "held": len(held),
            "released": released,
            "held_examples": [h["external_id"] for h in held][:12],
        }
    finally:
        _stop(sc)


def scenario_outage(settings: Settings) -> dict:
    sc = _start(settings, "outage", SCENARIOS["outage"])
    try:
        _run_samples(sc, replies=False)
        during = _collect(sc)
        during["customer_views"] = _customer_views(sc, ["m-1001"])
        inbox = sc.client.get(
            "/v1/shops/rb-downtown/inbox", headers=_headers(sc.settings, "rb-downtown")
        ).json()
        during["downtown_inbox_needs_action"] = sum(1 for i in inbox["items"] if i["needs_action"])
        # Recovery: provider back; staff press "retry" on each escalated conversation.
        sc.services.pipeline.provider = sc.services.provider.inner
        sc.services.breaker.record_success()
        retried = 0
        for shop in sc.services.catalog.shops:
            items = sc.client.get(f"/v1/shops/{shop}/inbox", headers=_headers(sc.settings, shop)).json()[
                "items"
            ]
            for item in items:
                if item["state"] == "needs_staff" and item["state_reason"] == "ai_unavailable":
                    r = sc.client.post(
                        f"/v1/shops/{shop}/conversations/{item['conversation_id']}/retry",
                        headers=_headers(sc.settings, shop),
                    )
                    retried += r.status_code == 200
        sc.services.worker().drain(max_wait_seconds=600)
        _send_replies(sc)
        sc.services.worker().drain(max_wait_seconds=600)
        after = _collect(sc)
        return {"during": during, "after_recovery": after, "retried": retried}
    finally:
        _stop(sc)


def _customer_views(sc: Scenario, first_ids: list[str]) -> dict:
    out = {}
    with sc.admin() as conn:
        for ext in first_ids:
            row = conn.execute(
                """SELECT c.shop_id, c.reply_token FROM conversations c JOIN inbound_messages m ON m.conversation_id = c.id
                   WHERE m.external_id = %s ORDER BY m.created_at LIMIT 1""",
                (ext,),
            ).fetchone()
            if row:
                out[ext] = sc.client.get(f"/v1/customer/{row['shop_id']}/{row['reply_token']}").json()
    return out


RUNNERS = {
    "baseline": scenario_baseline,
    "chaos": scenario_chaos,
    "budget": scenario_budget,
    "outage": scenario_outage,
}


def run_eval(settings: Settings, *, scenarios: list[str], out_path: str) -> int:
    results = {}
    for name in scenarios:
        print(f"== scenario {name} ...", flush=True)
        results[name] = RUNNERS[name](settings)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    provider = settings.llm_provider
    raw = REPO_ROOT / "results" / f"eval-{provider}-{stamp}.json"
    raw.parent.mkdir(exist_ok=True)
    raw.write_text(json.dumps(results, indent=1, default=str))
    from riverbend.report import render

    Path(out_path).write_text(render(results, raw.relative_to(REPO_ROOT).as_posix()))
    print(f"wrote {out_path} and {raw.relative_to(REPO_ROOT)}")
    base = results.get("baseline")
    if base:
        print(
            f"baseline: {base['work_orders']} work orders, {base['events'].get('follow_up_asked', 0)} follow-ups, "
            f"{base['events'].get('escalated', 0)} escalations, spend {format_money(int(Decimal(base['cost_usd']) * 100))}"
            f"{' (simulated)' if base['simulated_spend'] else ''}"
        )
    return 0
