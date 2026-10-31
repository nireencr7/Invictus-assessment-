"""Risk: concurrent workers jointly blow through a shop's monthly LLM cap, or the cap
silently drops customer messages. At the cap: no model call, message held and visible to
staff, customer already acknowledged, other shops unaffected, raising the cap releases it."""

import threading
from decimal import Decimal

from fastapi.testclient import TestClient

from riverbend.api import create_app
from tests.conftest import FakeProvider, offline_output, post_message

BODY = "Need new chain, old one is rusty. Road bike. Wednesday."
OK = offline_output("m-1010#1")
ADMIN = {"Authorization": "Bearer dev-admin-token"}


def test_concurrent_reservations_never_exceed_cap(make_services, admin):
    s = make_services(provider=FakeProvider(default=lambda r: OK))
    post_message(s, "rb-northside", "m-1", BODY)
    msg_id = admin.execute("SELECT id FROM inbound_messages").fetchone()["id"]
    s.budget.set_cap("rb-northside", Decimal("1.00"))
    granted, barrier = [], threading.Barrier(30)

    def reserve():
        barrier.wait()
        r = s.budget.reserve(
            shop_id="rb-northside",
            message_id=msg_id,
            job_id=None,
            purpose="extract",
            provider="fake",
            model="claude-opus-5-5",
            amount=Decimal("0.10"),
        )
        if r:
            granted.append(r)

    threads = [threading.Thread(target=reserve) for _ in range(30)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    row = admin.execute(
        "SELECT spent_usd, reserved_usd, cap_usd FROM shop_spend WHERE shop_id = 'rb-northside'"
    ).fetchone()
    assert len(granted) == 10
    assert row["spent_usd"] + row["reserved_usd"] <= row["cap_usd"]


def test_settle_books_actual_cost_and_releases_reservation(make_services, admin):
    s = make_services(provider=FakeProvider(default=lambda r: OK))
    post_message(s, "rb-northside", "m-1", BODY)
    s.worker().drain()
    call = admin.execute(
        "SELECT reserved_usd, cost_usd, input_tokens, output_tokens FROM llm_calls"
    ).fetchone()
    spend = admin.execute("SELECT spent_usd, reserved_usd FROM shop_spend").fetchone()
    # FakeProvider reports 2000 in / 400 out at Opus 5.5 prices: 2000*4/1e6 + 400*20/1e6 = 0.016
    assert call["cost_usd"] == Decimal("0.016000") < call["reserved_usd"]
    assert spend == {"spent_usd": Decimal("0.016000"), "reserved_usd": Decimal("0")}


def test_at_cap_message_is_held_not_dropped_and_no_call_is_made(make_services, admin):
    p = FakeProvider(default=lambda r: OK)
    s = make_services(provider=p)
    s.budget.set_cap("rb-northside", Decimal("0.0001"))
    post_message(s, "rb-northside", "m-N", BODY)
    post_message(s, "rb-downtown", "m-D", BODY)  # another shop, plenty of budget
    s.worker().drain()
    held = admin.execute("SELECT status FROM inbound_messages WHERE external_id = 'm-N'").fetchone()["status"]
    assert held == "held_budget"
    assert admin.execute(
        "SELECT status, park_reason FROM jobs WHERE shop_id = 'rb-northside'"
    ).fetchone() == {"status": "parked", "park_reason": "budget"}
    assert [c[0][0] for c in p.calls] == ["m-D"]  # Northside never reached the model
    assert (
        admin.execute("SELECT count(*) AS n FROM work_orders WHERE shop_id = 'rb-downtown'").fetchone()["n"]
        == 1
    )
    assert (
        admin.execute("SELECT count(*) AS n FROM events WHERE type = 'budget_exhausted'").fetchone()["n"] == 1
    )
    acks = admin.execute(
        "SELECT count(*) AS n FROM outbound_messages WHERE shop_id = 'rb-northside' AND kind = 'ack'"
    )
    assert acks.fetchone()["n"] == 1

    with TestClient(create_app(s.settings, s)) as client:
        staff = {"Authorization": "Bearer dev-northside-token"}
        assert client.get("/v1/shops/rb-northside/spend", headers=staff).json()["state"] == "exhausted"
        item = client.get("/v1/shops/rb-northside/inbox", headers=staff).json()["items"][0]
        assert item["needs_action"] and item["message_status"] == "held_budget"
        r = client.put("/v1/admin/shops/rb-northside/budget", headers=ADMIN, json={"monthly_cap_usd": "5"})
        assert r.json()["released_held_messages"] == 1 and r.json()["state"] == "ok"
    s.worker().drain()
    assert (
        admin.execute("SELECT count(*) AS n FROM work_orders WHERE shop_id = 'rb-northside'").fetchone()["n"]
        == 1
    )


def test_warning_event_at_80_percent(make_services, admin):
    s = make_services(provider=FakeProvider(default=lambda r: OK))
    post_message(s, "rb-northside", "m-1", BODY)
    msg_id = admin.execute("SELECT id FROM inbound_messages").fetchone()["id"]
    s.budget.set_cap("rb-northside", Decimal("1.00"))
    for _ in range(8):
        s.budget.reserve(
            shop_id="rb-northside",
            message_id=msg_id,
            job_id=None,
            purpose="extract",
            provider="fake",
            model="claude-opus-5-5",
            amount=Decimal("0.10"),
        )
    assert (
        admin.execute("SELECT count(*) AS n FROM events WHERE type = 'budget_warning'").fetchone()["n"] == 1
    )
    assert s.budget.status("rb-northside")["state"] == "warning"


def test_admin_endpoint_requires_admin_token(make_services):
    s = make_services(provider=FakeProvider(default=lambda r: OK))
    with TestClient(create_app(s.settings, s)) as client:
        r = client.put(
            "/v1/admin/shops/rb-northside/budget",
            headers={"Authorization": "Bearer dev-northside-token"},
            json={"monthly_cap_usd": "999"},
        )
        assert r.status_code == 401
