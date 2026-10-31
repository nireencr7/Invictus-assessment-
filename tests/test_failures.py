"""Risk: the provider is slow, down, or wrong, and a customer's request is lost, duplicated,
or turned into garbage. Expected behaviour:
- intake never depends on the model; the customer is acknowledged immediately;
- transient failures retry with backoff; a circuit breaker stops hammering a dead provider;
- after max attempts or the processing SLA, the message goes to staff — never silently dropped;
- invalid output is never saved; one repair attempt, then staff.
"""

from decimal import Decimal

from fastapi.testclient import TestClient

from riverbend.api import create_app
from riverbend.llm.base import PermanentProviderError, ProviderRefusal, TransientProviderError
from tests.conftest import FakeProvider, offline_output, post_message, reply_token

BODY = "Need new chain, old one is rusty. Road bike. Wednesday."
KEY = ("m-1", 1)
OK = offline_output("m-1010#1")


def state(admin):
    return admin.execute(
        "SELECT c.state, c.state_reason, m.status, m.outcome FROM conversations c JOIN inbound_messages m ON m.conversation_id = c.id"
    ).fetchone()


def wo_count(admin):
    return admin.execute("SELECT count(*) AS n FROM work_orders").fetchone()["n"]


def test_transient_failure_retries_then_succeeds(make_services, admin):
    p = FakeProvider(script={KEY: [TransientProviderError("503"), TransientProviderError("529"), OK]})
    s = make_services(provider=p)
    post_message(s, "rb-lakeshore", "m-1", BODY)
    s.worker().drain()
    assert wo_count(admin) == 1 and len(p.calls) == 3
    assert admin.execute("SELECT attempts FROM jobs").fetchone()["attempts"] == 3
    statuses = [r["status"] for r in admin.execute("SELECT status FROM llm_calls ORDER BY id").fetchall()]
    assert statuses == ["transient_error", "transient_error", "ok"]


def test_persistent_outage_hands_to_staff_and_customer_keeps_ack(make_services, admin):
    p = FakeProvider(default=lambda r: TransientProviderError("down"))
    s = make_services(provider=p, max_attempts=3)
    res = post_message(s, "rb-lakeshore", "m-1", BODY)
    assert res.status_code == 202  # intake does not wait for the model
    s.worker().drain()
    st = state(admin)
    assert (st["state"], st["state_reason"], st["status"]) == ("needs_staff", "ai_unavailable", "needs_staff")
    assert admin.execute("SELECT status FROM jobs").fetchone()["status"] == "dead"
    assert wo_count(admin) == 0 and len(p.calls) == 3
    with TestClient(create_app(s.settings, s)) as client:
        token = reply_token(admin, res.conversation_id)
        view = client.get(f"/v1/customer/rb-lakeshore/{token}").json()
        assert view["status"] == "A team member is reviewing your request personally."
        assert [m["kind"] for m in view["messages"]] == ["ack"]
        inbox = client.get(
            "/v1/shops/rb-lakeshore/inbox", headers={"Authorization": "Bearer dev-lakeshore-token"}
        ).json()
        assert inbox["items"][0]["needs_action"] and inbox["items"][0]["body"] == BODY
        # Provider recovers; staff press retry.
        p.default = lambda r: OK
        cid = res.conversation_id
        r = client.post(
            f"/v1/shops/rb-lakeshore/conversations/{cid}/retry",
            headers={"Authorization": "Bearer dev-lakeshore-token"},
        )
        assert r.status_code == 200
    s.worker().drain()
    assert wo_count(admin) == 1


def test_circuit_breaker_defers_without_burning_attempts(make_services, admin):
    p = FakeProvider(default=lambda r: TransientProviderError("down"))
    s = make_services(
        provider=p,
        breaker_failure_threshold=2,
        breaker_cooldown_seconds=0.15,
        processing_sla_seconds=0.6,
        max_attempts=50,
    )
    for i in range(5):
        post_message(s, "rb-downtown", f"m-{i}", BODY)
    outcomes = s.worker().drain()
    assert outcomes.get("deferred_breaker_open", 0) > 0
    # Without the breaker every job would retry until the SLA; with it, far fewer calls are made.
    assert len(p.calls) < 5 * 6
    reasons = {r["state_reason"] for r in admin.execute("SELECT state_reason FROM conversations").fetchall()}
    assert reasons == {"ai_unavailable"}


def test_timeout_is_booked_at_worst_case_cost(make_services, admin):
    p = FakeProvider(script={KEY: [TransientProviderError("timeout", billed_unknown=True), OK]})
    s = make_services(provider=p)
    post_message(s, "rb-lakeshore", "m-1", BODY)
    s.worker().drain()
    first = admin.execute(
        "SELECT status, cost_usd, reserved_usd FROM llm_calls ORDER BY id LIMIT 1"
    ).fetchone()
    assert first["status"] == "timeout_unknown"
    assert first["cost_usd"] == first["reserved_usd"] > 0


def test_permanent_error_goes_to_staff_without_retrying(make_services, admin):
    p = FakeProvider(default=lambda r: PermanentProviderError("401 invalid x-api-key"))
    s = make_services(provider=p)
    post_message(s, "rb-lakeshore", "m-1", BODY)
    s.worker().drain()
    assert len(p.calls) == 1
    assert state(admin)["state_reason"] == "ai_configuration_error"


def test_refusal_goes_to_staff(make_services, admin):
    s = make_services(provider=FakeProvider(default=lambda r: ProviderRefusal("refused")))
    post_message(s, "rb-lakeshore", "m-1", BODY)
    s.worker().drain()
    assert state(admin)["state_reason"] == "model_refused"


def test_invalid_output_is_repaired_once(make_services, admin):
    bad = {**OK, "unit_price": 0}
    p = FakeProvider(script={KEY: [bad, OK]})
    s = make_services(provider=p)
    post_message(s, "rb-lakeshore", "m-1", BODY)
    s.worker().drain()
    assert wo_count(admin) == 1
    repair_request = p.calls[1][1]
    assert (
        repair_request.messages[-1]["role"] == "user"
        and "unit_price" in repair_request.messages[-1]["content"]
    )
    purposes = [
        (r["purpose"], r["status"])
        for r in admin.execute("SELECT purpose, status FROM llm_calls ORDER BY id")
    ]
    assert purposes == [("extract", "invalid_output"), ("repair", "ok")]


def test_invalid_twice_is_never_saved(make_services, admin):
    s = make_services(provider=FakeProvider(default=lambda r: '{"intent": "repair_request", "price": 0'))
    post_message(s, "rb-lakeshore", "m-1", BODY)
    s.worker().drain()
    assert wo_count(admin) == 0
    assert state(admin)["state_reason"] == "invalid_model_output"
    raw = admin.execute(
        "SELECT raw_output FROM llm_calls WHERE status = 'invalid_output' LIMIT 1"
    ).fetchone()["raw_output"]
    assert raw.startswith('{"intent"')  # kept for debugging, never as a work order


def test_ungrounded_service_is_rejected_end_to_end(make_services, admin):
    invented = {
        **OK,
        "services": [
            *OK["services"],
            {
                "code": "TUNE_FULL",
                "wheel": "not_applicable",
                "bikes": 1,
                "evidence": "please do a full overhaul too",
                "reason": "upsell",
            },
        ],
    }
    s = make_services(provider=FakeProvider(script={KEY: [invented, OK]}))
    post_message(s, "rb-lakeshore", "m-1", BODY)
    s.worker().drain()
    codes = [r["service_code"] for r in admin.execute("SELECT service_code FROM work_order_lines")]
    assert codes == ["CHAIN_REPLACE"]


def test_worker_crash_is_retried_then_dead_lettered(make_services, admin):
    def boom(req):
        raise RuntimeError("bug in our code")

    s = make_services(provider=FakeProvider(default=boom), max_attempts=2)
    post_message(s, "rb-lakeshore", "m-1", BODY)
    s.worker().drain()
    assert admin.execute("SELECT status FROM jobs").fetchone()["status"] == "dead"
    # The reservation for the crashed call was left open; the sweeper books it conservatively.
    assert s.budget.sweep_orphans(older_than_seconds=0) >= 1
    spend = admin.execute("SELECT spent_usd, reserved_usd FROM shop_spend").fetchone()
    assert spend["reserved_usd"] == Decimal(0) and spend["spent_usd"] > 0
