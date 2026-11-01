"""A message handed to staff tells the customer so, and staff can answer from the console."""

import pytest
from fastapi.testclient import TestClient

from riverbend.api import create_app

DOWN = {"Authorization": "Bearer dev-downtown-token"}
LAKE = {"Authorization": "Bearer dev-lakeshore-token"}


@pytest.fixture
def client(make_services):
    s = make_services()  # offline stand-in: unseen messages go through its keyword heuristic
    with TestClient(create_app(s.settings, s)) as c:
        c.services = s
        yield c


def say(client, body, reply_to=None, shop="rb-downtown", headers=DOWN):
    r = client.post(
        f"/v1/shops/{shop}/test-messages",
        headers=headers,
        json={
            "customer_email": "jamie@example.com",
            "body": body,
            **({"reply_to": reply_to} if reply_to else {}),
        },
    )
    assert r.status_code == 202, r.text
    client.services.worker().drain()
    return r.json()["conversation_id"]


def conversation(client, conv, shop="rb-downtown", headers=DOWN):
    return client.get(f"/v1/shops/{shop}/conversations/{conv}", headers=headers).json()


def reply(client, conv, body, **kw):
    return client.post(
        f"/v1/shops/rb-downtown/conversations/{conv}/reply", headers=DOWN, json={"body": body, **kw}
    )


def kinds(detail):
    return [o["kind"] for o in detail["outbound"]]


def test_question_after_the_work_order_goes_to_staff_and_staff_answer(client):
    """The exact path from the console: order made, then "is it negotiable?"."""
    conv = say(client, "My front tyre keeps going flat and the gears slip. Can you fix it?")
    say(client, "Saturday morning works for me.", reply_to=conv)
    assert conversation(client, conv)["conversation"]["state"] == "work_order_created"

    say(client, "is it negotiable?", reply_to=conv)
    d = conversation(client, conv)
    assert (d["conversation"]["state"], d["conversation"]["state_reason"]) == (
        "needs_staff",
        "customer_wrote_after_conversation_closed",
    )
    assert kinds(d) == ["ack", "follow_up", "summary", "handoff"]
    assert len(d["llm_calls"]) == 2  # the model is not asked about a finished order

    say(client, "hello? anyone there?", reply_to=conv)
    assert kinds(conversation(client, conv)).count("handoff") == 1  # not one per message while waiting

    r = reply(
        client, conv, "Hi Jamie, our prices follow the service menu, so the $40 stands.", client_id="abc12345"
    )
    assert r.json() == {"sent": True, "duplicate": False, "state": "work_order_created", "resolved": True}
    d = conversation(client, conv)
    assert d["conversation"]["state"] == "work_order_created" and kinds(d)[-1] == "staff_reply"
    assert {m["status"] for m in d["inbound"]} == {"processed"}
    assert d["events"][-1]["type"] == "staff_replied" and d["events"][-1]["detail"]["resolved"] is True
    inbox = client.get("/v1/shops/rb-downtown/inbox", headers=DOWN).json()["items"]
    assert not any(i["needs_action"] for i in inbox)

    # A retried request with the same client_id is one message, not two.
    again = reply(
        client, conv, "Hi Jamie, our prices follow the service menu, so the $40 stands.", client_id="abc12345"
    )
    assert again.json()["duplicate"] is True and kinds(conversation(client, conv)).count("staff_reply") == 1

    # The customer sees the hand-off note and the reply.
    db = client.services.db  # reply tokens never leave the database through the staff API
    with db.tenant_tx("rb-downtown") as conn:
        rt = conn.execute("SELECT reply_token FROM conversations WHERE id = %s", (conv,)).fetchone()[
            "reply_token"
        ]
    seen = client.get(f"/v1/customer/rb-downtown/{rt}").json()
    assert [m["kind"] for m in seen["messages"]][-2:] == ["handoff", "staff_reply"]


def test_staff_reply_without_a_work_order_marks_it_answered(client):
    conv = say(client, "Please cancel my booking for Saturday, something came up.")
    d = conversation(client, conv)
    assert d["conversation"]["state_reason"] == "cancellation_needs_match" and kinds(d) == ["ack", "handoff"]
    assert reply(client, conv, "Which day was your booking for?").json()["state"] == "answered"
    assert conversation(client, conv)["conversation"]["state_reason"] == "staff_replied"


def test_staff_can_write_without_a_hand_off(client):
    conv = say(client, "Need a new chain, old one is rusty. Wednesday.")
    r = reply(client, conv, "See you Wednesday!")
    assert r.json() == {"sent": True, "duplicate": False, "state": "work_order_created", "resolved": False}


def test_staff_reply_validation_and_isolation(client):
    lake = say(client, "Flat tyre. Friday.", shop="rb-lakeshore", headers=LAKE)
    assert reply(client, lake, "hi").status_code == 404  # another shop's conversation
    conv = say(client, "Flat tyre. Friday.")
    assert reply(client, conv, "   ").status_code == 422
    assert reply(client, conv, "hi", client_id="no spaces!").status_code == 422
    assert reply(client, conv, "hi", shop_id="rb-lakeshore").status_code == 422
    assert (
        client.post(f"/v1/shops/rb-downtown/conversations/{conv}/reply", json={"body": "hi"}).status_code
        == 401
    )
