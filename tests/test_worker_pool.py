"""Risk: "One shop's burst makes every other shop wait", and "more workers process a job twice".

1. Fair claiming: a free worker serves the shop with the fewest jobs running, oldest first
   within the shop, so a shop with one message is served next even behind a flood.
2. The worker pool sizes itself on the backlog (1, 2, 3 threads) and shrinks only after the
   backlog has stayed low, and real threads sharing one queue still process each job once.
"""

import threading
import time

from riverbend import jobqueue
from riverbend.worker import WorkerPool
from tests.conftest import FakeProvider, offline_output, post_message

DOWN, NORTH, LAKE = "rb-downtown", "rb-northside", "rb-lakeshore"
BODY = "Need new chain, old one is rusty. Road bike. Wednesday."


def queue(s, shop, n):
    for i in range(n):
        post_message(s, shop, f"{shop}-{i}", f"{BODY} ({i})")


def test_free_worker_serves_the_shop_with_fewest_jobs_running(make_services):
    s = make_services()
    queue(s, DOWN, 5)  # arrived first
    queue(s, NORTH, 2)
    queue(s, LAKE, 1)
    # Claim without finishing, as busy workers would: each claim sees the others still running.
    shops = []
    while (job := jobqueue.claim(s.db, lease_seconds=60)) is not None:
        shops.append(job.shop_id)
    assert shops == [DOWN, NORTH, LAKE, DOWN, NORTH, DOWN, DOWN, DOWN]


def test_workers_freeing_up_at_the_same_instant_still_take_different_shops(make_services):
    s = make_services()
    queue(s, DOWN, 5)
    queue(s, NORTH, 2)
    queue(s, LAKE, 1)
    claimed, barrier = [], threading.Barrier(3)

    def claim():
        barrier.wait()
        claimed.append(jobqueue.claim(s.db, lease_seconds=60).shop_id)

    threads = [threading.Thread(target=claim) for _ in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    # Without claims taking turns, all three would count 0 running and all pick Downtown.
    assert sorted(claimed) == sorted([DOWN, NORTH, LAKE])


def test_a_flood_from_one_shop_does_not_hold_up_another(make_services):
    s = make_services(provider=FakeProvider(default=lambda r: offline_output("m-1010#1")))
    queue(s, DOWN, 10)
    queue(s, LAKE, 1)
    worker = s.worker()
    order = []
    while worker.run_once() is not None:
        order.append(worker.last_job.shop_id)
    # One worker, oldest-first would serve Lakeshore 11th. Fair claiming serves it 2nd.
    assert order[:2] == [DOWN, LAKE] and order.count(DOWN) == 10


class _Stub:
    """Stands in for a Worker thread: runs until told to stop."""

    def __init__(self, name):
        self.name = name
        self._stop = threading.Event()

    def run_forever(self):
        self._stop.wait()

    def stop(self):
        self._stop.set()


def test_pool_adds_threads_at_once_and_removes_them_slowly(monkeypatch):
    backlog, now = [0], [0.0]
    monkeypatch.setattr(jobqueue, "ready_count", lambda db: backlog[0])
    pool = WorkerPool(_Stub, db=None, scale_down_seconds=60, clock=lambda: now[0])
    assert [pool.target(n) for n in (0, 25, 26, 50, 51, 500)] == [1, 1, 2, 2, 3, 3]

    def at(t, waiting):
        now[0], backlog[0] = t, waiting
        return pool.adjust()

    assert at(0, 10) == 1
    assert at(5, 30) == 2  # more than 25 waiting
    assert at(10, 60) == 3  # more than 50
    assert at(15, 900) == 3  # never above the maximum
    assert at(100, 10) == 3  # lower: the timer starts
    assert at(130, 10) == 3
    assert at(160, 10) == 2  # 60 s lower: one thread stops after its job
    assert at(170, 40) == 2  # target is 2 now: steady
    assert at(175, 10) == 2  # lower again: a new 60 s timer
    assert at(235, 0) == 1
    assert at(400, 0) == 1  # never below the minimum
    pool.stop()
    pool._shutdown(timeout=5)
    assert all(not t.is_alive() for t in pool._retiring)


def test_pool_threads_share_one_queue_and_process_each_job_once(make_services, admin):
    s = make_services(
        provider=FakeProvider(default=lambda r: offline_output("m-1010#1")),
        worker_poll_seconds=0.02,
        worker_scale_check_seconds=0.05,
    )
    queue(s, DOWN, 20)
    queue(s, NORTH, 10)
    queue(s, LAKE, 30)  # 60 waiting: three threads
    pool = s.worker_pool()
    runner = threading.Thread(target=pool.run_forever)
    runner.start()
    try:
        deadline = time.monotonic() + 60
        peak = 0
        while time.monotonic() < deadline:
            peak = max(peak, pool.size)
            if admin.execute("SELECT count(*) AS n FROM jobs WHERE status <> 'done'").fetchone()["n"] == 0:
                break
            time.sleep(0.05)
    finally:
        pool.stop()
        runner.join(timeout=30)
    assert peak == 3
    assert admin.execute("SELECT count(*) AS n FROM jobs WHERE status = 'done'").fetchone()["n"] == 60
    # Exactly one extraction call per message: no job was processed twice.
    per_message = admin.execute(
        "SELECT count(*) AS n FROM llm_calls WHERE purpose = 'extract' GROUP BY message_id"
    ).fetchall()
    assert len(per_message) == 60 and {r["n"] for r in per_message} == {1}
    assert admin.execute("SELECT count(*) AS n FROM work_orders").fetchone()["n"] == 60
