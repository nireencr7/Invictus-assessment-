"""Exports a read-only dataset for the staff console's snapshot mode.

Everything in the file is a real API response from a real run of the pipeline (offline
stand-in provider), captured through the same endpoints the console calls live:

1. the 40 brief samples, with a customer reply to m-1027's follow-up question
   (m-1009's wrong-shop question is left unanswered, so it waits in the inbox);
2. one extra message sent to Downtown while the provider is down (-> handed to staff);
3. one extra message sent to Northside after its cap is lowered (-> held at the cap);
4. two demo customers with email addresses, sent through the staff test tool, so the Customers
   view has someone to show: Jamie (a follow-up answered; then "is the price negotiable?" on the
   finished order, handed to staff and answered by staff; then a price question) and Priya
   (a follow-up still waiting).

    uv run python -m riverbend snapshot            # writes ui/public/demo-snapshot.json
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from riverbend.config import REPO_ROOT, Settings
from riverbend.evaluation import (
    REPLIES,
    Scenario,
    _collect,
    _headers,
    _post_samples,
    _start,
    _stop,
)
from riverbend.llm.resilience import FaultInjectingProvider, FaultPlan

DEMO_OUTAGE = {
    "shop": "rb-downtown",
    "message_id": "demo-outage-1",
    "body": "Rear derailleur hanger snapped on my gravel bike. Can I bring it in Friday? Mia",
}
DEMO_CAP = {
    "shop": "rb-northside",
    "message_id": "demo-cap-1",
    "body": "My headset creaks when I brake hard. Thursday works. Sven",
}


DEMO_CUSTOMERS = [
    ("rb-downtown", "jamie.lee@example.com", "Jamie Lee",
     "My front tyre keeps going flat and the gears slip. Can you fix it?", "Saturday morning works for me."),
    ("rb-downtown", "jamie.lee@example.com", "Jamie Lee", "How much do you charge to true a wheel?", None),
    ("rb-lakeshore", "priya.shah@example.com", "Priya Shah", "Hi, can I book my bike in for next week?", None),
]  # fmt: skip


DEMO_AFTER_ORDER = (
    "Is the price negotiable?",
    "Hi Jamie, our prices follow the service menu, so the $40.00 estimate stands. If the tube turns out "
    "to be fine, we'll only charge for the gear adjustment.",
)


def _demo_customers(sc: Scenario) -> None:
    worker = sc.services.worker()

    def as_customer(shop: str, h: dict, message: dict) -> str:
        r = sc.client.post(f"/v1/shops/{shop}/test-messages", headers=h, json=message)
        assert r.status_code == 202, r.text
        worker.drain(max_wait_seconds=120)
        return r.json()["conversation_id"]

    for i, (shop, email, name, body, reply) in enumerate(DEMO_CUSTOMERS):
        h = _headers(sc.settings, shop)
        conv = as_customer(shop, h, {"customer_email": email, "customer_name": name, "body": body})
        if reply:
            as_customer(shop, h, {"customer_email": email, "body": reply, "reply_to": conv})
        if i == 0:
            # A question about the finished order goes to a person, who answers from the console.
            question, answer = DEMO_AFTER_ORDER
            as_customer(shop, h, {"customer_email": email, "body": question, "reply_to": conv})
            r = sc.client.post(
                f"/v1/shops/{shop}/conversations/{conv}/reply", headers=h, json={"body": answer}
            )
            assert r.json()["resolved"], r.text


def _post(sc: Scenario, shop: str, message_id: str, body: str, reply_token: str | None = None) -> dict:
    payload = {
        "message_id": message_id,
        "channel": "email",
        "received_at": "2026-09-22T13:05:00",
        "body": body,
    }
    if reply_token:
        payload["reply_token"] = reply_token
    r = sc.client.post(f"/v1/shops/{shop}/inbound-messages", headers=_headers(sc.settings), json=payload)
    assert r.status_code in (200, 202), r.text
    return r.json()


def _reply_to(sc: Scenario, first_external_id: str) -> None:
    with sc.admin() as conn:
        row = conn.execute(
            """SELECT c.shop_id, c.reply_token FROM conversations c
               JOIN inbound_messages m ON m.conversation_id = c.id
               WHERE m.external_id = %s AND c.state = 'awaiting_customer'""",
            (first_external_id,),
        ).fetchone()
    if row:
        replies = json.loads(REPLIES.read_text())["replies"]
        _post(
            sc, row["shop_id"], f"{first_external_id}-reply", replies[first_external_id], row["reply_token"]
        )


def _export_shop(sc: Scenario, shop_id: str) -> dict:
    h = _headers(sc.settings, shop_id)
    get = lambda path: sc.client.get(path, headers=h).json()  # noqa: E731
    out = {
        "workOrders": get(f"/v1/shops/{shop_id}/work-orders?status=all"),
        "inbox": get(f"/v1/shops/{shop_id}/inbox"),
        "spend": get(f"/v1/shops/{shop_id}/spend"),
        "llmCalls": get(f"/v1/shops/{shop_id}/llm-calls?limit=500"),
        "activity": get(f"/v1/shops/{shop_id}/activity?limit=500"),
        "conversations": {},
        "customers": get(f"/v1/shops/{shop_id}/customers"),
        "customerHistory": {},
    }
    for c in out["customers"]["customers"]:
        out["customerHistory"][c["id"]] = get(f"/v1/shops/{shop_id}/customers/{c['id']}")
    with sc.admin() as conn:
        convs = conn.execute(
            "SELECT id::text AS id, reply_token FROM conversations WHERE shop_id = %s", (shop_id,)
        ).fetchall()
    for c in convs:
        detail = get(f"/v1/shops/{shop_id}/conversations/{c['id']}")
        # What the customer sees, fetched through the customer route; the token itself is not exported.
        detail["customerView"] = sc.client.get(f"/v1/customer/{shop_id}/{c['reply_token']}").json()
        out["conversations"][c["id"]] = detail
    return out


def _eval_summary() -> dict | None:
    files = sorted((REPO_ROOT / "results").glob("eval-*.json"))
    if not files:
        return None
    data = json.loads(files[-1].read_text())
    keep = (
        "provider", "model", "simulated_spend", "messages_received", "replies", "duplicates",
        "conversations", "work_orders", "cancel_requested", "calls", "calls_by_status", "input_tokens",
        "output_tokens", "cost_usd", "retries", "events", "outbound", "rows", "injected",
    )  # fmt: skip

    def slim(r: dict | None) -> dict | None:
        return None if r is None else {k: r[k] for k in keep if k in r}

    budget, outage = data.get("budget"), data.get("outage")
    return {
        "source": files[-1].name,
        "baseline": slim(data.get("baseline")),
        "chaos": slim(data.get("chaos")),
        "budget": None
        if not budget
        else {
            "held": budget["held"],
            "released": budget["released"],
            "heldExamples": budget["held_examples"],
            "spendAtCap": budget["spend_at_cap"],
            "workOrdersAfterRaise": budget["after_raise"]["work_orders"],
        },
        "outage": None
        if not outage
        else {
            "acks": outage["during"]["outbound"].get("ack", 0),
            "workOrdersDuring": outage["during"]["work_orders"],
            "costDuring": outage["during"]["cost_usd"],
            "handedToStaff": sum(1 for r in outage["during"]["rows"] if r["state"] == "needs_staff"),
            "retried": outage["retried"],
            "workOrdersAfterRecovery": outage["after_recovery"]["work_orders"],
        },
    }


def export_snapshot(settings: Settings, out_path: Path) -> dict:
    sc = _start(
        settings,
        "snapshot",
        {"processing_sla_seconds": 1.0, "max_attempts": 3, "breaker_failure_threshold": 2,
         "breaker_cooldown_seconds": 0.2, "test_messages": True},
    )  # fmt: skip
    try:
        worker = sc.services.worker()
        _post_samples(sc)
        worker.drain(max_wait_seconds=600)
        _reply_to(sc, "m-1027")  # m-1009 stays unanswered: a realistic "waiting on customer"
        worker.drain(max_wait_seconds=600)

        # A message that arrives while the provider is down.
        healthy = sc.services.pipeline.provider
        sc.services.pipeline.provider = FaultInjectingProvider(healthy, FaultPlan.parse("outage"))
        _post(sc, DEMO_OUTAGE["shop"], DEMO_OUTAGE["message_id"], DEMO_OUTAGE["body"])
        worker.drain(max_wait_seconds=120)
        sc.services.pipeline.provider = healthy
        sc.services.breaker.record_success()

        # A message that arrives after Northside's cap is lowered below its next reservation.
        spent = Decimal(sc.client.get("/v1/shops/rb-northside/spend", headers=_headers(sc.settings, "rb-northside"))
                        .json()["spent_usd"])  # fmt: skip
        cap = str((spent + Decimal("0.01")).quantize(Decimal("0.01")))
        sc.client.put("/v1/admin/shops/rb-northside/budget", headers=_headers(sc.settings, admin=True),
                      json={"monthly_cap_usd": cap})  # fmt: skip
        _post(sc, DEMO_CAP["shop"], DEMO_CAP["message_id"], DEMO_CAP["body"])
        worker.drain(max_wait_seconds=120)

        _demo_customers(sc)

        collected = _collect(sc)
        snapshot = {
            "generatedAt": datetime.now(UTC).isoformat(timespec="seconds"),
            "provider": sc.services.provider.name,
            "model": sc.services.provider.model,
            "simulatedSpend": collected["simulated_spend"],
            "note": (
                "The 40 brief samples processed by the offline stand-in (hand-written outputs, not a model), "
                "plus two extra messages (one sent during a simulated provider outage, one after Northside's "
                "cap was lowered) and two demo customers sent through the staff test tool. "
                "Every record is a real API response from that run."
            ),
            "shops": [{"id": s.id, "name": s.name} for s in sc.services.catalog.shops.values()],
            "byShop": {shop_id: _export_shop(sc, shop_id) for shop_id in sc.services.catalog.shops},
            "eval": _eval_summary(),
        }
    finally:
        _stop(sc)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(snapshot, indent=1, default=str) + "\n")
    return snapshot
