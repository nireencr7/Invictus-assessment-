"""The worker loop: claim a job, process it, repeat. Run as many processes as you like;
SKIP LOCKED and the job lease make concurrent workers safe."""

from __future__ import annotations

import logging
import signal
import threading
import time

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
    ):
        self.db = db
        self.pipeline = pipeline
        self.budget = budget
        self.lease_seconds = lease_seconds
        self.poll_seconds = poll_seconds
        self.housekeeping_seconds = housekeeping_seconds
        self._stop = threading.Event()
        self._last_housekeeping = 0.0
        self.outcomes: dict[str, int] = {}

    def stop(self) -> None:
        self._stop.set()

    def run_once(self) -> str | None:
        """Process at most one job. Returns its outcome, or None if nothing was ready."""
        job = jobqueue.claim(self.db, self.lease_seconds)
        if job is None:
            return None
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
        log.info("job %s shop=%s attempt=%s -> %s", job.id, job.shop_id, job.attempts, outcome)
        return outcome

    def housekeeping(self) -> None:
        swept = self.budget.sweep_orphans(older_than_seconds=self.lease_seconds * 2)
        if swept:
            log.warning("booked %s orphaned budget reservations", swept)

    def run_forever(self) -> None:
        log.info(
            "worker started (provider=%s model=%s)", self.pipeline.provider.name, self.pipeline.provider.model
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


def install_signal_handlers(worker: Worker) -> None:
    def handler(signum, _frame):
        log.info("signal %s: finishing current job then stopping", signum)
        worker.stop()

    signal.signal(signal.SIGTERM, handler)
    signal.signal(signal.SIGINT, handler)
