"""A Postgres-backed job queue.

Why not Pub/Sub or Cloud Tasks? Accepting a message and enqueueing its job happen in the
*same transaction* as the idempotent insert, so there is no dual-write window where a
message is stored but never processed (or processed but not stored). At Riverbend's volume
(hundreds of messages a day) Postgres is not the bottleneck. See docs/ARCHITECTURE.md.

Delivery is at-least-once. Workers claim with FOR UPDATE SKIP LOCKED and a lease; a job
whose worker dies is re-claimed when the lease expires. Completing a job checks the lease
token, so a worker that lost its lease cannot also commit results.
"""

from __future__ import annotations

import random
import uuid
from dataclasses import dataclass
from datetime import datetime

from riverbend.db import Database


class LeaseLost(Exception):
    """Another worker now owns this job; abandon (and roll back) our results."""


@dataclass(frozen=True)
class Job:
    id: int
    shop_id: str
    message_id: uuid.UUID
    attempts: int
    lease_token: uuid.UUID
    last_error: str | None
    sla_started_at: datetime


def enqueue(conn, shop_id: str, message_id) -> None:
    conn.execute(
        "INSERT INTO jobs (shop_id, message_id) VALUES (%s, %s) ON CONFLICT (shop_id, message_id) DO NOTHING",
        (shop_id, message_id),
    )


def claim(db: Database, lease_seconds: float) -> Job | None:
    token = uuid.uuid4()
    with db.system_tx() as conn:
        row = conn.execute(
            """UPDATE jobs SET status = 'running', attempts = attempts + 1, lease_token = %(t)s,
                      locked_until = now() + make_interval(secs => %(lease)s), updated_at = now()
               WHERE id = (
                   SELECT id FROM jobs
                   WHERE (status = 'queued' AND run_after <= now())
                      OR (status = 'running' AND locked_until < now())
                   ORDER BY run_after, id
                   FOR UPDATE SKIP LOCKED
                   LIMIT 1)
               RETURNING id, shop_id, message_id, attempts, lease_token, last_error, sla_started_at""",
            {"t": token, "lease": lease_seconds},
        ).fetchone()
    return Job(**row) if row else None


def complete(conn, job: Job, status: str = "done", error: str | None = None) -> None:
    """Finish the job inside the caller's (tenant) transaction, so results and completion
    commit together. Raises LeaseLost if someone else re-claimed it meanwhile."""
    row = conn.execute(
        """UPDATE jobs SET status = %s, lease_token = NULL, locked_until = NULL,
                  last_error = COALESCE(%s, last_error), updated_at = now()
           WHERE id = %s AND lease_token = %s RETURNING id""",
        (status, error, job.id, job.lease_token),
    ).fetchone()
    if row is None:
        raise LeaseLost(f"job {job.id} lease lost")


def reschedule(
    db: Database, job: Job, delay_seconds: float, error: str | None, *, consume_attempt: bool
) -> None:
    with db.system_tx() as conn:
        conn.execute(
            """UPDATE jobs SET status = 'queued', run_after = now() + make_interval(secs => %s),
                      attempts = attempts - %s, last_error = %s, lease_token = NULL, locked_until = NULL,
                      updated_at = now()
               WHERE id = %s AND lease_token = %s""",
            (delay_seconds, 0 if consume_attempt else 1, error, job.id, job.lease_token),
        )


def park(conn, job: Job, reason: str) -> None:
    conn.execute(
        """UPDATE jobs SET status = 'parked', park_reason = %s, lease_token = NULL, locked_until = NULL,
                  attempts = attempts - 1, updated_at = now()
           WHERE id = %s AND lease_token = %s""",
        (reason, job.id, job.lease_token),
    )


def unpark(db: Database, shop_id: str, reason: str) -> int:
    with db.system_tx() as conn:
        rows = conn.execute(
            """UPDATE jobs SET status = 'queued', park_reason = NULL, last_error = NULL, run_after = now(),
                      sla_started_at = now(),
                      updated_at = now()
               WHERE shop_id = %s AND status = 'parked' AND park_reason = %s RETURNING id""",
            (shop_id, reason),
        ).fetchall()
    return len(rows)


def requeue(conn, shop_id: str, message_id) -> bool:
    """Staff 'try the AI again' on a dead-lettered message."""
    row = conn.execute(
        """UPDATE jobs SET status = 'queued', attempts = 0, last_error = NULL, run_after = now(),
                  sla_started_at = now(), updated_at = now()
           WHERE shop_id = %s AND message_id = %s AND status IN ('dead', 'parked') RETURNING id""",
        (shop_id, message_id),
    ).fetchone()
    return row is not None


def backoff_seconds(attempt: int, base: float, cap: float, rng: random.Random | None = None) -> float:
    """Exponential backoff with full jitter: uniform(0.5, 1.0) * min(cap, base * 2^(attempt-1))."""
    rng = rng or random
    raw = min(cap, base * (2 ** max(0, attempt - 1)))
    return raw * (0.5 + rng.random() / 2)
