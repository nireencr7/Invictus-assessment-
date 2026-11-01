"""Customers (by email, per shop), their full history, and the staff "send as a customer" test tool."""

import psycopg
import pytest
from fastapi.testclient import TestClient

from riverbend.api import create_app
from riverbend.intake import InboundPayload, ingest, parse_sender
from tests.conftest import as_app, post_message

DOWN = {"Authorization": "Bearer dev-downtown-token"}
LAKE = {"Authorization": "Bearer dev-lakeshore-token"}


@pytest.fixture
def client(make_services):
    # The offline stand-in: unseen messages go through its keyword heuristic, like a real run.
    s = make_services()
    with TestClient(create_app(s.settings, s)) as c:
        c.services = s
        yield c


def send(client, shop="rb-downtown", headers=DOWN, **body):
    return client.post(f"/v1/shops/{shop}/test-messages", headers=headers, json=body)


def test_parse_sender():
    assert parse_sender("Jamie Lee <Jamie.Lee@Example.com>") == ("jamie.lee@example.com", "Jamie Lee")
    assert parse_sender("jamie@example.com") == ("jamie@example.com", None)
    assert parse_sender("not an address") == (None, None)
    assert parse_sender(None) == (None, None)


def test_one_customer_per_address_per_shop(client, admin):
    s = client.services
    first = post_message(s, "rb-downtown", "m-1", "My brakes squeal. Thursday.")  # no sender at all

    def ingest_from(shop, mid, sender):
        p = {
            "message_id": mid,
            "channel": "email",
            "received_at": "2026-09-22T08:00:00",
            "body": "Flat tyre.",
            "sender": sender,
        }
        return ingest(s.db, shop, InboundPayload(**p))

    ingest_from("rb-downtown", "m-2", "Jamie Lee <Jamie@Example.com>")
    ingest_from("rb-downtown", "m-3", "jamie@example.com")
    ingest_from("rb-lakeshore", "m-4", "jamie@example.com")

    rows = admin.execute("SELECT shop_id, email, display_name FROM customers ORDER BY shop_id").fetchall()
    assert [(r["shop_id"], r["email"], r["display_name"]) for r in rows] == [
        ("rb-downtown", "jamie@example.com", "Jamie Lee"),  # a later bare address keeps the known name
        ("rb-lakeshore", "jamie@example.com", None),
    ]
    down = client.get("/v1/shops/rb-downtown/customers", headers=DOWN).json()
    assert down["count"] == 1 and down["customers"][0]["conversations"] == 2
    assert down["conversations_without_email"] == 1  # m-1 had no sender
    assert first.conversation_id not in {
        c["id"]
        for c in client.get(
            f"/v1/shops/rb-downtown/customers/{down['customers'][0]['id']}", headers=DOWN
        ).json()["conversations"]
    }


def test_customers_are_isolated_between_shops(client, db_urls):
    lake = send(
        client, "rb-lakeshore", LAKE, customer_email="ravi@example.com", body="New chain please. Wednesday."
    )
    assert lake.status_code == 202
    lake_customer = lake.json()["customer_id"]
    assert client.get("/v1/shops/rb-downtown/customers", headers=DOWN).json()["count"] == 0
    # Another shop's customer id through your own shop: not found. Your token on their shop: not found.
    assert client.get(f"/v1/shops/rb-downtown/customers/{lake_customer}", headers=DOWN).status_code == 404
    assert client.get(f"/v1/shops/rb-lakeshore/customers/{lake_customer}", headers=DOWN).status_code == 404
    # And the database agrees, whatever the API does.
    with psycopg.connect(db_urls.app) as conn:
        assert conn.execute("SELECT count(*) FROM customers").fetchone()[0] == 0
    db = as_app(db_urls)
    try:
        with db.tenant_tx("rb-downtown") as conn:
            assert conn.execute("SELECT count(*) AS n FROM customers").fetchone()["n"] == 0
    finally:
        db.close()


def test_send_as_customer_then_reply_to_the_follow_up(client):
    """The whole loop from the console: new request -> one question -> customer replies -> work order."""
    r = send(
        client,
        customer_email="Jamie.Lee@example.com",
        customer_name="Jamie Lee",
        body="My front tyre keeps going flat and the gears slip. Can you fix it?",
    )
    assert r.status_code == 202, r.text
    first = r.json()
    assert first["external_id"].startswith("test-") and not first["reply"]
    client.services.worker().drain()

    history = client.get(f"/v1/shops/rb-downtown/customers/{first['customer_id']}", headers=DOWN).json()
    assert (
        history["customer"]["email"] == "jamie.lee@example.com" and history["customer"]["name"] == "Jamie Lee"
    )
    assert history["customer"]["awaiting_reply"] == 1
    [conv] = history["conversations"]
    assert conv["state"] == "awaiting_customer" and conv["work_order"] is None

    r = send(
        client,
        customer_email="jamie.lee@example.com",
        body="Saturday morning works for me.",
        reply_to=conv["id"],
    )
    assert r.status_code == 202 and r.json()["reply"] and r.json()["conversation_id"] == conv["id"]
    client.services.worker().drain()

    history = client.get(f"/v1/shops/rb-downtown/customers/{first['customer_id']}", headers=DOWN).json()
    [conv] = history["conversations"]
    assert conv["state"] == "work_order_created" and conv["work_order"]["status"] == "open"
    assert history["customer"]["open_orders"] == 1 and history["customer"]["open_estimate"] != "$0.00"
    assert history["model_calls"] == 2

    kinds = [(t["kind"], t.get("type") or t.get("message_kind")) for t in history["timeline"]]
    # Each message first, then what the system did, then what the customer was sent.
    assert (
        kinds.index(("customer_message", None))
        < kinds.index(("event", "received"))
        < kinds.index(("shop_message", "ack"))
    )
    for step in [
        ("event", "follow_up_asked"),
        ("shop_message", "follow_up"),
        ("event", "work_order_created"),
    ]:
        assert step in kinds, step
    received = [t for t in history["timeline"] if t.get("type") == "received"]
    assert [t["detail"]["via"] for t in received] == ["staff test", "staff test"]
    assert "reply_token" not in str(history)


def test_status_changes_show_in_the_customer_history(client):
    r = send(client, customer_email="ravi@example.com", body="Need a new chain, old one is rusty. Wednesday.")
    client.services.worker().drain()
    customer = r.json()["customer_id"]
    [conv] = client.get(f"/v1/shops/rb-downtown/customers/{customer}", headers=DOWN).json()["conversations"]
    wo = conv["work_order"]["id"]
    assert (
        client.post(
            f"/v1/shops/rb-downtown/work-orders/{wo}/status", headers=DOWN, json={"status": "completed"}
        ).status_code
        == 200
    )
    timeline = client.get(f"/v1/shops/rb-downtown/customers/{customer}", headers=DOWN).json()["timeline"]
    assert timeline[-1]["type"] == "work_order_status" and timeline[-1]["detail"]["status"] == "completed"


def test_test_message_validation(client):
    assert send(client, customer_email="not-an-email", body="hi").status_code == 422
    assert send(client, customer_email="Jamie <j@example.com>", body="hi").status_code == 422
    assert send(client, customer_email="j@example.com", body="   ").status_code == 422
    assert send(client, customer_email="j@example.com", body="hi", shop_id="rb-lakeshore").status_code == 422
    assert send(client, customer_email="j@example.com", body="hi", reply_to="nope").status_code == 404
    lake = send(
        client, "rb-lakeshore", LAKE, customer_email="j@example.com", body="Flat tyre. Friday."
    ).json()
    # Replying into another shop's conversation is a 404, not a cross-tenant write.
    assert (
        send(client, customer_email="j@example.com", body="hi", reply_to=lake["conversation_id"]).status_code
        == 404
    )
    assert (
        client.post(
            "/v1/shops/rb-downtown/test-messages", json={"customer_email": "j@example.com", "body": "hi"}
        ).status_code
        == 401
    )


def test_test_messages_are_off_in_production_unless_switched_on(make_services):
    for enabled in (False, True):
        s = make_services(dev_mode=False, test_messages=enabled)
        with TestClient(create_app(s.settings, s)) as c:
            assert c.get("/v1/me", headers=DOWN).json()["features"] == {"test_messages": enabled}
            r = c.post(
                "/v1/shops/rb-downtown/test-messages",
                headers=DOWN,
                json={"customer_email": "j@example.com", "body": "Flat tyre. Friday."},
            )
            assert r.status_code == (202 if enabled else 404)
