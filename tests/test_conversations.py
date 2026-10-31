"""Risk: follow-ups loop, lose context, or cross tenant lines.
"Asks the customer one follow-up question when a required field is missing or ambiguous,
and resumes when they reply.\""""

import psycopg
import pytest

from tests.conftest import FakeProvider, offline_output, post_message, reply_token


def outbound(admin, conversation_id):
    return [
        r["kind"]
        for r in admin.execute(
            "SELECT kind FROM outbound_messages WHERE conversation_id = %s ORDER BY created_at",
            (conversation_id,),
        )
    ]


def test_follow_up_then_resume_on_reply(make_services, admin):
    p = FakeProvider(
        script={("m-1027", 1): [offline_output("m-1027#1")], ("m-1027", 2): [offline_output("m-1027#2")]}
    )
    s = make_services(provider=p)
    first = post_message(s, "rb-downtown", "m-1027", "Brakes squeal.", channel="web")
    s.worker().drain()
    conv = admin.execute("SELECT state, follow_ups_asked FROM conversations").fetchone()
    assert conv == {"state": "awaiting_customer", "follow_ups_asked": 1}
    token = reply_token(admin, first.conversation_id)
    second = post_message(
        s,
        "rb-downtown",
        "m-1027-r",
        "Thursday afternoon if possible. It's a road bike.",
        reply_token=token,
        received="2026-09-22T11:20:00",
    )
    assert second.conversation_id == first.conversation_id
    s.worker().drain()
    wo = admin.execute("SELECT requested_earliest, estimate_cents FROM work_orders").fetchone()
    assert str(wo["requested_earliest"]) == "2026-09-24" and wo["estimate_cents"] == 3600
    assert outbound(admin, first.conversation_id) == ["ack", "follow_up", "summary"]
    # The model saw the whole thread on the second turn.
    prompt = p.calls[-1][1].messages[0]["content"]
    assert "<customer_message>" in prompt and "<shop_question>" in prompt and "<customer_reply>" in prompt


def test_database_enforces_a_single_follow_up(make_services, admin):
    s = make_services(provider=FakeProvider(default=lambda r: offline_output("m-1027#1")))
    post_message(s, "rb-downtown", "m-1027", "Brakes squeal.")
    s.worker().drain()
    with pytest.raises(psycopg.errors.CheckViolation):
        admin.execute("UPDATE conversations SET follow_ups_asked = follow_ups_asked + 1")


def test_still_missing_after_reply_creates_flagged_order_not_second_question(make_services, admin):
    still_no_date = offline_output("m-1027#1")  # model again says: no date, ask
    s = make_services(provider=FakeProvider(default=lambda r: still_no_date))
    first = post_message(s, "rb-downtown", "m-1027", "Brakes squeal.")
    s.worker().drain()
    post_message(
        s,
        "rb-downtown",
        "m-1027-r",
        "Not sure yet, I'll see.",
        reply_token=reply_token(admin, first.conversation_id),
    )
    s.worker().drain()
    assert outbound(admin, first.conversation_id).count("follow_up") == 1
    reasons = admin.execute("SELECT review_reasons FROM work_orders").fetchone()["review_reasons"]
    assert "no_date_after_follow_up" in reasons


def test_wrong_shop_redirect_never_moves_data_across_tenants(make_services, admin):
    wants_lakeshore = {**offline_output("m-1009#1"), "shop_confirmation": "wants_other_shop"}
    p = FakeProvider(script={("m-1009", 1): [offline_output("m-1009#1")], ("m-1009", 2): [wants_lakeshore]})
    s = make_services(provider=p)
    body = "Hi Lakeshore team! Following up on my visit last week. Can you also replace both tires on my gravel bike? Sunday works. Jo"
    first = post_message(s, "rb-downtown", "m-1009", body)
    s.worker().drain()
    follow = admin.execute("SELECT body FROM outbound_messages WHERE kind = 'follow_up'").fetchone()["body"]
    assert "Riverbend Cycles Downtown" in follow and "Riverbend Cycles Lakeshore" in follow
    post_message(
        s,
        "rb-downtown",
        "m-1009-r",
        "Oh, I meant Lakeshore, sorry!",
        reply_token=reply_token(admin, first.conversation_id),
    )
    s.worker().drain()
    assert admin.execute("SELECT state FROM conversations").fetchone()["state"] == "redirected"
    redirect = admin.execute("SELECT body FROM outbound_messages WHERE kind = 'redirect'").fetchone()["body"]
    assert "lakeshore@riverbendcycles.example" in redirect
    # Nothing was written to Lakeshore's tenant.
    assert (
        admin.execute("SELECT count(*) AS n FROM inbound_messages WHERE shop_id = 'rb-lakeshore'").fetchone()[
            "n"
        ]
        == 0
    )
    assert admin.execute("SELECT count(*) AS n FROM work_orders").fetchone()["n"] == 0


def test_cancellation_matches_the_one_open_order_in_the_same_shop(make_services, admin):
    p = FakeProvider(
        script={("m-1003", 1): [offline_output("m-1003#1")], ("m-1038", 1): [offline_output("m-1038#1")]}
    )
    s = make_services(provider=p)
    post_message(
        s,
        "rb-lakeshore",
        "m-1003",
        "The chain on my hybrid keeps falling off when I shift to the small ring. Free Saturday. Marcus",
    )
    s.worker().drain()
    post_message(
        s,
        "rb-lakeshore",
        "m-1038",
        "Hi, it's Marcus again. Please cancel the chain appointment I asked about this morning, I fixed it myself.",
    )
    s.worker().drain()
    assert admin.execute("SELECT status FROM work_orders").fetchone()["status"] == "cancel_requested"
    assert (
        admin.execute(
            "SELECT count(*) AS n FROM outbound_messages WHERE kind = 'cancellation_ack'"
        ).fetchone()["n"]
        == 1
    )


def test_cancellation_never_matches_another_shops_customer(make_services, admin):
    p = FakeProvider(
        script={("m-1003", 1): [offline_output("m-1003#1")], ("m-1038", 1): [offline_output("m-1038#1")]}
    )
    s = make_services(provider=p)
    post_message(
        s,
        "rb-lakeshore",
        "m-1003",
        "The chain on my hybrid keeps falling off when I shift to the small ring. Free Saturday. Marcus",
    )
    s.worker().drain()
    post_message(
        s,
        "rb-downtown",
        "m-1038",
        "Hi, it's Marcus again. Please cancel the chain appointment I asked about this morning, I fixed it myself.",
    )
    s.worker().drain()
    assert admin.execute("SELECT status FROM work_orders").fetchone()["status"] == "open"
    reason = admin.execute("SELECT state_reason FROM conversations WHERE shop_id = 'rb-downtown'").fetchone()
    assert reason["state_reason"] == "cancellation_needs_match"


def test_message_on_finished_conversation_goes_to_staff_without_a_model_call(make_services, admin):
    p = FakeProvider(default=lambda r: offline_output("m-1010#1"))
    s = make_services(provider=p)
    first = post_message(
        s, "rb-lakeshore", "m-1010", "Need new chain, old one is rusty. Road bike. Wednesday."
    )
    s.worker().drain()
    post_message(
        s,
        "rb-lakeshore",
        "m-1010-r",
        "Thanks! Also can you check my brakes?",
        reply_token=reply_token(admin, first.conversation_id),
    )
    s.worker().drain()
    assert len(p.calls) == 1
    assert (
        admin.execute("SELECT state_reason FROM conversations").fetchone()["state_reason"]
        == "customer_wrote_after_conversation_closed"
    )
