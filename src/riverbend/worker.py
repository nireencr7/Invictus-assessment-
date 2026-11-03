"""The worker loop: claim a job, process it, repeat. Run as many as you like, as threads or
processes; SKIP LOCKED and the job lease make concurrent workers safe. WorkerPool runs 1-3 of
them in one process, sized by how many jobs are waiting."""

from __future__ import annotations

import logging
import math
import signal
import threading
import time
from collections.abc import Callable

from riverbend import jobqueue
from riverbend.budget import Budget
from riverbend.db import Database
from riverbend.pipeline import Pipeline

log = logging.getLogger(__name__)


class Worker:
    def __init__(
        self,
        db: Database,
        pipeline: Pipeline,
        budget: Budget,
        *,
        lease_seconds: float,
        poll_seconds: float = 0.5,
        housekeeping_seconds: float = 30.0,
        name: str = "worker",
    ):
        self.name = name
        self.db = db
        self.pipeline = pipeline
        self.budget = budget
        self.lease_seconds = lease_seconds
        self.poll_seconds = poll_seconds
        self.housekeeping_seconds = housekeeping_seconds
        self._stop = threading.Event()
        self._last_housekeeping = 0.0
        self.outcomes: dict[str, int] = {}
        self.last_job: jobqueue.Job | None = None  # the job the latest run_once claimed (dev step mode)

    def stop(self) -> None:
        self._stop.set()

    def run_once(self) -> str | None:
        """Process at most one job. Returns its outcome, or None if nothing was ready."""
        job = jobqueue.claim(self.db, self.lease_seconds)
        if job is None:
            return None
        self.last_job = job
        try:
            outcome = self.pipeline.process(job)
        except Exception as exc:  # a bug, not a provider failure: retry with backoff, then dead-letter
            log.exception("job %s crashed", job.id)
            if job.attempts >= self.pipeline.settings.max_attempts:
                with self.db.system_tx() as conn:
                    conn.execute(
                        "UPDATE jobs SET status = 'dead', last_error = %s WHERE id = %s",
                        (f"crash: {exc}"[:500], job.id),
                    )
                outcome = "dead_after_crash"
            else:
                jobqueue.reschedule(
                    self.db, job, 1.0 * job.attempts, f"crash: {exc}"[:500], consume_attempt=True
                )
                outcome = "crash_retry"
        key = outcome.split(":")[0]
        self.outcomes[key] = self.outcomes.get(key, 0) + 1
        log.info("%s: job %s shop=%s attempt=%s -> %s", self.name, job.id, job.shop_id, job.attempts, outcome)
        return outcome

    def housekeeping(self) -> None:
        swept = self.budget.sweep_orphans(older_than_seconds=self.lease_seconds * 2)
        if swept:
            log.warning("booked %s orphaned budget reservations", swept)

    def run_forever(self) -> None:
        log.info(
            "%s started (provider=%s model=%s)",
            self.name,
            self.pipeline.provider.name,
            self.pipeline.provider.model,
        )
        while not self._stop.is_set():
            now = time.monotonic()
            if now - self._last_housekeeping > self.housekeeping_seconds:
                self._last_housekeeping = now
                try:
                    self.housekeeping()
                except Exception:
                    log.exception("housekeeping failed")
            try:
                did = self.run_once()
            except Exception:
                log.exception("worker loop error (database unavailable?)")
                did = None
                self._stop.wait(2.0)
            if did is None:
                self._stop.wait(self.poll_seconds)

    def drain(self, *, max_wait_seconds: float = 120.0) -> dict[str, int]:
        """Run until no job is runnable or will become runnable soon (tests and the eval harness)."""
        deadline = time.monotonic() + max_wait_seconds
        while time.monotonic() < deadline:
            if self.run_once() is not None:
                continue
            with self.db.system_tx() as conn:
                row = conn.execute(
                    """SELECT EXTRACT(EPOCH FROM (min(run_after) - now())) AS wait FROM jobs
                       WHERE status = 'queued' OR (status = 'running' AND locked_until IS NOT NULL)"""
                ).fetchone()
            if row["wait"] is None:
                return self.outcomes
            time.sleep(min(max(float(row["wait"]), 0.01), 1.0))
        raise TimeoutError("queue did not drain")


class WorkerPool:
    """Runs between min and max Worker threads in this process, sized by the backlog of ready jobs:
    one thread per `jobs_per_thread` waiting. Every thread is the same Worker loop; fair claiming
    (jobqueue.claim) shares them across shops. Threads are added as soon as the backlog grows and
    removed one at a time once it has stayed lower for `scale_down_seconds`; a removed thread
    finishes its current job first."""

    def __init__(
        self,
        make_worker: Callable[[str], Worker],
        db: Database,
        *,
        min_threads: int = 1,
        max_threads: int = 3,
        jobs_per_thread: int = 25,
        check_seconds: float = 5.0,
        scale_down_seconds: float = 60.0,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.make_worker = make_worker
        self.db = db
        self.min_threads = min_threads
        self.max_threads = max(min_threads, max_threads)
        self.jobs_per_thread = jobs_per_thread
        self.check_seconds = check_seconds
        self.scale_down_seconds = scale_down_seconds
        self.clock = clock
        self._stop = threading.Event()
        self._active: list[tuple[Worker, threading.Thread]] = []
        self._retiring: list[threading.Thread] = []
        self._lower_since: float | None = None
        self._started = 0

    @property
    def size(self) -> int:
        return len(self._active)

    def target(self, backlog: int) -> int:
        return max(self.min_threads, min(self.max_threads, math.ceil(backlog / self.jobs_per_thread)))

    def adjust(self) -> int:
        """One scaling check. Returns the number of active threads afterwards."""
        self._active = [
            (w, t) for w, t in self._active if t.is_alive()
        ]  # a thread that died is replaced below
        backlog = jobqueue.ready_count(self.db)
        want, have = self.target(backlog), self.size
        if want > have:
            for _ in range(want - have):
                self._start()
            self._lower_since = None
            log.info("backlog %s: %s -> %s worker threads", backlog, have, self.size)
        elif want < have:
            now = self.clock()
            if self._lower_since is None:
                self._lower_since = now
            elif now - self._lower_since >= self.scale_down_seconds:
                worker, thread = self._active.pop()
                worker.stop()  # finishes the job in hand, then exits
                self._retiring.append(thread)
                self._lower_since = now
                log.info(
                    "backlog %s: %s -> %s worker threads (%s stops after its job)",
                    backlog,
                    have,
                    self.size,
                    worker.name,
                )
        else:
            self._lower_since = None
        return self.size

    def _start(self) -> None:
        self._started += 1
        worker = self.make_worker(f"worker-{self._started}")
        thread = threading.Thread(target=worker.run_forever, name=worker.name, daemon=True)
        thread.start()
        self._active.append((worker, thread))

    def run_forever(self) -> None:
        log.info(
            "worker pool started: %s-%s threads, one per %s waiting jobs",
            self.min_threads,
            self.max_threads,
            self.jobs_per_thread,
        )
        while not self._stop.is_set():
            try:
                self.adjust()
            except Exception:
                log.exception("worker pool: scaling check failed (database unavailable?)")
                if not self._active:  # never leave the queue with nobody working on it
                    self._start()
            self._stop.wait(self.check_seconds)
        self._shutdown()

    def stop(self) -> None:
        self._stop.set()

    def _shutdown(self, timeout: float = 30.0) -> None:
        for worker, _ in self._active:
            worker.stop()
        deadline = time.monotonic() + timeout
        for thread in [t for _, t in self._active] + self._retiring:
            thread.join(timeout=max(0.0, deadline - time.monotonic()))


def install_signal_handlers(worker: Worker | WorkerPool) -> None:
    def handler(signum, _frame):
        log.info("signal %s: finishing current job then stopping", signum)
        worker.stop()

    signal.signal(signal.SIGTERM, handler)
    signal.signal(signal.SIGINT, handler)
