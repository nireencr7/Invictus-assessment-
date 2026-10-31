"""The HTTP contract: intake validation and auth, the work-order list, the customer view."""

import pytest
from fastapi.testclient import TestClient

from riverbend.api import create_app
from tests.conftest import FakeProvider, offline_output, payload

INGEST = {"X-Ingest-Key": "dev-ingest-key"}
LAKE = {"Authorization": "Bearer dev-lakeshore-token"}


@pytest.fixture
def client(make_services):
    outputs = {"m-1029": offline_output("m-1029#1"), "m-1010": offline_output("m-1010#1")}
    s = make_services(provider=FakeProvider(default=lambda r: outputs[r.metadata["conversation_key"]]))
    with TestClient(create_app(s.settings, s)) as c:
        c.services = s
        yield c


def post(client, shop, mid, body, headers=INGEST, **kw):
    return client.post(f"/v1/shops/{shop}/inbound-messages", headers=headers, json=payload(mid, body, **kw))


def test_intake_auth_and_validation(client):
    body = "Need new chain, old one is rusty. Road bike. Wednesday."
    assert post(client, "rb-lakeshore", "m-1", body, headers={}).status_code == 401
    assert post(client, "rb-lakeshore", "m-1", body, headers={"X-Ingest-Key": "wrong"}).status_code == 401
    assert post(client, "rb-nowhere", "m-1", body).status_code == 403
    assert post(client, "rb-lakeshore", "m-1", "   ").status_code == 422
    r = client.post(
        "/v1/shops/rb-lakeshore/inbound-messages",
        headers=INGEST,
        json={**payload("m-1", body), "shop_id": "rb-downtown"},
    )
    assert r.status_code == 422  # unknown fields are rejected, not ignored
    assert post(client, "rb-lakeshore", "m-1", body).status_code == 202


def test_work_order_list_with_estimates(client):
    post(
        client,
        "rb-lakeshore",
        "m-1029",
        "New pads on both wheels and bleed both brakes please, mountain bike. Saturday. Ravi",
    )
    post(client, "rb-lakeshore", "m-1010", "Need new chain, old one is rusty. Road bike. Wednesday.")
    client.services.worker().drain()
    data = client.get("/v1/shops/rb-lakeshore/work-orders", headers=LAKE).json()
    assert data["count"] == 2 and data["open_estimate_total"] == "$195.00"
    ravi = next(w for w in data["work_orders"] if w["customer_name"] == "Ravi")
    assert [(line["code"], line["quantity"], line["line_total"]) for line in ravi["lines"]] == [
        ("BRAKE_PADS", 2, "$70.00"),
        ("BRAKE_BLEED", 2, "$80.00"),
    ]
    assert ravi["estimate"] == {
        "cents": 15000,
        "formatted": "$150.00",
        "currency": "USD",
        "provisional": False,
    }
    assert ravi["requested"]["earliest"] == "2026-09-26"

    # Staff closes one; the open list shrinks.
    r = client.post(
        f"/v1/shops/rb-lakeshore/work-orders/{ravi['id']}/status", headers=LAKE, json={"status": "completed"}
    )
    assert r.status_code == 200
    assert client.get("/v1/shops/rb-lakeshore/work-orders", headers=LAKE).json()["count"] == 1
    assert client.get("/v1/shops/rb-lakeshore/work-orders?status=all", headers=LAKE).json()["count"] == 2


def test_customer_view_is_scoped_by_token_and_shop(client, admin):
    r = post(client, "rb-lakeshore", "m-1010", "Need new chain, old one is rusty. Road bike. Wednesday.")
    client.services.worker().drain()
    token = admin.execute("SELECT reply_token FROM conversations").fetchone()["reply_token"]
    view = client.get(f"/v1/customer/rb-lakeshore/{token}").json()
    assert [m["kind"] for m in view["messages"]] == ["ack", "summary"]
    assert "$45.00" in view["messages"][1]["body"]
    assert client.get(f"/v1/customer/rb-downtown/{token}").status_code == 404  # same token, other tenant
    assert client.get("/v1/customer/rb-lakeshore/not-a-token").status_code == 404
    # Staff timeline never exposes the reply token.
    conv = client.get(
        f"/v1/shops/rb-lakeshore/conversations/{r.json()['conversation_id']}", headers=LAKE
    ).json()
    assert "reply_token" not in conv["conversation"]


def test_health(client):
    assert client.get("/healthz").json() == {"ok": True}
    assert client.get("/readyz").json()["ok"] is True
