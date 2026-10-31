"""Risk: "The same message can arrive twice; it must produce one work order."

Three independent layers, each tested on its own:
1. intake: UNIQUE (shop_id, external_id) — the duplicate is recognised at the door;
2. queue: UNIQUE (shop_id, message_id) on jobs — one job per message;
3. write: UNIQUE (shop_id, conversation_id) on work orders + lease-checked completion —
   even if a job is processed twice (crash, expired lease), one work order.
"""

import threading

from riverbend import jobqueue
from tests.conftest import FakeProvider, offline_output, post_message

BODY = "The chain on my hybrid keeps falling off when I shift to the small ring. Free Saturday. Marcus"


def counts(admin):
    q = lambda sql: admin.execute(sql).fetchone()["n"]  # noqa: E731
    return {
        "messages": q("SELECT count(*) AS n FROM inbound_messages"),
        "jobs": q("SELECT count(*) AS n FROM jobs"),
        "work_orders": q("SELECT count(*) AS n FROM work_orders"),
        "summaries": q("SELECT count(*) AS n FROM outbound_messages WHERE kind = 'summary'"),
        "acks": q("SELECT count(*) AS n FROM outbound_messages WHERE kind = 'ack'"),
    }


def fake():
    return FakeProvider(default=lambda req: offline_output("m-1003#1"))


def test_duplicate_delivery_makes_one_work_order(make_services, admin):
    provider = fake()
    s = make_services(provider=provider)
    first = post_message(s, "rb-lakeshore", "m-1003", BODY, received="2026-09-22T08:15:00")
    s.worker().drain()
    second = post_message(s, "rb-lakeshore", "m-1003", BODY, received="2026-09-22T09:22:00")
    s.worker().drain()
    assert (first.status_code, second.status_code) == (202, 200)
    assert second.duplicate and second.message_id == first.message_id
    assert counts(admin) == {"messages": 1, "jobs": 1, "work_orders": 1, "summaries": 1, "acks": 1}
    assert len(provider.calls) == 1  # the duplicate never reached the model
    assert (
        admin.execute("SELECT duplicate_deliveries FROM inbound_messages").fetchone()["duplicate_deliveries"]
        == 1
    )


def test_duplicate_before_processing_is_also_one(make_services, admin):
    s = make_services(provider=fake())
    post_message(s, "rb-lakeshore", "m-1003", BODY)
    post_message(s, "rb-lakeshore", "m-1003", BODY)
    s.worker().drain()
    assert counts(admin)["work_orders"] == 1


def test_same_id_different_body_is_a_conflict(make_services):
    s = make_services(provider=fake())
    post_message(s, "rb-lakeshore", "m-1003", BODY)
    r = post_message(s, "rb-lakeshore", "m-1003", "Completely different text")
    assert r.status_code == 409


def test_idempotency_key_is_per_shop(make_services, admin):
    s = make_services(provider=fake())
    a = post_message(s, "rb-lakeshore", "m-1", BODY)
    b = post_message(s, "rb-downtown", "m-1", BODY)
    assert (a.status_code, b.status_code) == (202, 202)  # different tenants, no cross-tenant lookup
    assert counts(admin)["messages"] == 2


def test_concurrent_duplicate_deliveries(make_services, admin):
    s = make_services(provider=fake())
    results, barrier = [], threading.Barrier(8)

    def deliver():
        barrier.wait()
        results.append(post_message(s, "rb-lakeshore", "m-race", BODY).status_code)

    threads = [threading.Thread(target=deliver) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    s.worker().drain()
    assert sorted(results) == [200] * 7 + [202]
    assert counts(admin) == {"messages": 1, "jobs": 1, "work_orders": 1, "summaries": 1, "acks": 1}


def test_job_processed_twice_after_lease_expiry_makes_one_work_order(make_services, admin):
    """Worker A is slow; its lease expires; worker B re-claims and finishes. Then A finishes."""
    holder = {}

    def slow_then_steal(req):
        # While A is "waiting on the model", its lease expires and B processes the same job.
        if "stolen" not in holder:
            holder["stolen"] = True
            admin.execute("UPDATE jobs SET locked_until = now() - interval '1 second'")
            job_b = jobqueue.claim(s.db, 30)
            assert job_b is not None and job_b.id == holder["job_a"].id
            holder["b_outcome"] = s.pipeline.process(job_b)
        return offline_output("m-1003#1")

    s = make_services(provider=FakeProvider(default=slow_then_steal))
    post_message(s, "rb-lakeshore", "m-1003", BODY)
    job_a = jobqueue.claim(s.db, 30)
    holder["job_a"] = job_a
    outcome_a = s.pipeline.process(job_a)
    assert holder["b_outcome"] == "work_order_created"
    assert outcome_a == "lease_lost"  # A's results were rolled back
    assert counts(admin)["work_orders"] == 1
    assert counts(admin)["summaries"] == 1
    assert admin.execute("SELECT status FROM jobs").fetchone()["status"] == "done"


def test_redelivered_job_after_commit_is_a_no_op(make_services, admin):
    provider = fake()
    s = make_services(provider=provider)
    post_message(s, "rb-lakeshore", "m-1003", BODY)
    s.worker().drain()
    # Simulate at-least-once delivery handing us the same job again.
    admin.execute("UPDATE jobs SET status = 'queued', run_after = now()")
    assert s.worker().run_once() == "already_processed"
    assert counts(admin)["work_orders"] == 1 and len(provider.calls) == 1
