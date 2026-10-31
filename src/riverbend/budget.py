"""Per-shop monthly LLM spend cap.

Reserve-then-settle, like a card pre-authorisation:
1. Before a call, reserve its *worst-case* cost with one conditional UPDATE:
       spent + reserved + estimate <= cap
   Row-level locking makes this atomic, so N concurrent workers cannot jointly overshoot.
2. After the call, release the reservation and book the actual cost from the provider's
   usage numbers. If the outcome is unknown (client-side timeout: the provider may have
   billed us), book the full reservation — we would rather under-spend the cap than lie.
3. A sweeper books reservations orphaned by a crashed worker, the same conservative way.

At the cap, no call is made. The message is held, staff see it in their inbox, and the
customer already has an acknowledgement. Raising the cap (or the 1st of the month)
releases held messages.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal

from psycopg.types.json import Jsonb

from riverbend.db import Database
from riverbend.llm.base import Usage

log = logging.getLogger(__name__)


def month_of(now: datetime) -> date:
    return now.astimezone(UTC).date().replace(day=1)


@dataclass(frozen=True)
class Reservation:
    call_id: int
    shop_id: str
    month: date
    amount: Decimal


class Budget:
    def __init__(self, db: Database, warn_ratio: Decimal, clock=lambda: datetime.now(UTC)):
        self.db = db
        self.warn_ratio = warn_ratio
        self.clock = clock

    @staticmethod
    def _ensure_month_row(conn, shop_id: str, month: date) -> None:
        conn.execute(
            """INSERT INTO shop_spend (shop_id, month, cap_usd)
               SELECT id, %s, monthly_llm_cap_usd FROM shops WHERE id = %s
               ON CONFLICT (shop_id, month) DO NOTHING""",
            (month, shop_id),
        )

    def reserve(
        self,
        *,
        shop_id: str,
        message_id,
        job_id: int | None,
        purpose: str,
        provider: str,
        model: str,
        amount: Decimal,
    ) -> Reservation | None:
        month = month_of(self.clock())
        with self.db.tenant_tx(shop_id) as conn:
            self._ensure_month_row(conn, shop_id, month)
            row = conn.execute(
                """UPDATE shop_spend SET reserved_usd = reserved_usd + %(a)s
                   WHERE shop_id = %(s)s AND month = %(m)s AND spent_usd + reserved_usd + %(a)s <= cap_usd
                   RETURNING spent_usd, reserved_usd, cap_usd, warned_at""",
                {"a": amount, "s": shop_id, "m": month},
            ).fetchone()
            if row is None:
                first = conn.execute(
                    """UPDATE shop_spend SET exhausted_at = now()
                       WHERE shop_id = %s AND month = %s AND exhausted_at IS NULL RETURNING cap_usd""",
                    (shop_id, month),
                ).fetchone()
                if first:
                    log.warning("shop %s reached its monthly LLM cap (%s USD)", shop_id, first["cap_usd"])
                    conn.execute(
                        "INSERT INTO events (shop_id, message_id, type, detail) VALUES (%s, %s, 'budget_exhausted', %s)",
                        (
                            shop_id,
                            message_id,
                            Jsonb({"cap_usd": str(first["cap_usd"]), "month": month.isoformat()}),
                        ),
                    )
                return None
            if (
                row["warned_at"] is None
                and row["spent_usd"] + row["reserved_usd"] >= row["cap_usd"] * self.warn_ratio
            ):
                conn.execute(
                    "UPDATE shop_spend SET warned_at = now() WHERE shop_id = %s AND month = %s",
                    (shop_id, month),
                )
                conn.execute(
                    "INSERT INTO events (shop_id, message_id, type, detail) VALUES (%s, %s, 'budget_warning', %s)",
                    (
                        shop_id,
                        message_id,
                        Jsonb({"ratio": str(self.warn_ratio), "cap_usd": str(row["cap_usd"])}),
                    ),
                )
            call = conn.execute(
                """INSERT INTO llm_calls (shop_id, message_id, job_id, purpose, provider, model, status, month, reserved_usd)
                   VALUES (%s, %s, %s, %s, %s, %s, 'reserved', %s, %s) RETURNING id""",
                (shop_id, message_id, job_id, purpose, provider, model, month, amount),
            ).fetchone()
        return Reservation(call["id"], shop_id, month, amount)

    def settle(
        self,
        res: Reservation,
        *,
        status: str,
        cost: Decimal | None,
        usage: Usage | None = None,
        latency_ms: int | None = None,
        request_id: str | None = None,
        error: str | None = None,
        raw_output: str | None = None,
    ) -> Decimal:
        """Book the call. `cost=None` means unknown: book the full reservation."""
        charged = res.amount if cost is None else cost
        u = usage or Usage()
        with self.db.tenant_tx(res.shop_id) as conn:
            updated = conn.execute(
                """UPDATE llm_calls SET status = %s, cost_usd = %s, input_tokens = %s, output_tokens = %s,
                          cache_read_tokens = %s, cache_write_tokens = %s, latency_ms = %s, request_id = %s,
                          error = %s, raw_output = %s, settled_at = now()
                   WHERE shop_id = %s AND id = %s AND status = 'reserved' RETURNING id""",
                (
                    status,
                    charged,
                    u.input_tokens,
                    u.output_tokens,
                    u.cache_read_tokens,
                    u.cache_write_tokens,
                    latency_ms,
                    request_id,
                    error,
                    raw_output,
                    res.shop_id,
                    res.call_id,
                ),
            ).fetchone()
            if updated is None:
                return Decimal(0)  # already booked by the sweeper
            conn.execute(
                """UPDATE shop_spend SET reserved_usd = GREATEST(reserved_usd - %s, 0), spent_usd = spent_usd + %s
                   WHERE shop_id = %s AND month = %s""",
                (res.amount, charged, res.shop_id, res.month),
            )
        return charged

    def sweep_orphans(self, older_than_seconds: float) -> int:
        """Book reservations left behind by a crashed worker at their full amount."""
        with self.db.system_tx() as conn:
            shops = [r["id"] for r in conn.execute("SELECT id FROM shops").fetchall()]
        n = 0
        for shop_id in shops:
            with self.db.tenant_tx(shop_id) as conn:
                rows = conn.execute(
                    """SELECT id, month, reserved_usd FROM llm_calls
                       WHERE status = 'reserved' AND created_at < now() - make_interval(secs => %s)""",
                    (older_than_seconds,),
                ).fetchall()
            for r in rows:
                self.settle(
                    Reservation(r["id"], shop_id, r["month"], r["reserved_usd"]),
                    status="expired",
                    cost=None,
                    error="reservation orphaned; booked at worst case",
                )
                n += 1
        return n

    def status(self, shop_id: str) -> dict:
        month = month_of(self.clock())
        with self.db.tenant_tx(shop_id) as conn:
            self._ensure_month_row(conn, shop_id, month)
            row = conn.execute(
                "SELECT * FROM shop_spend WHERE shop_id = %s AND month = %s", (shop_id, month)
            ).fetchone()
            calls = conn.execute(
                """SELECT count(*) AS calls, COALESCE(sum(input_tokens), 0) AS input_tokens,
                          COALESCE(sum(output_tokens), 0) AS output_tokens
                   FROM llm_calls WHERE month = %s""",
                (month,),
            ).fetchone()
        cap, spent, reserved = row["cap_usd"], row["spent_usd"], row["reserved_usd"]
        return {
            "shop_id": shop_id,
            "month": month.isoformat(),
            "cap_usd": str(cap),
            "spent_usd": str(spent),
            "reserved_usd": str(reserved),
            "remaining_usd": str(max(cap - spent - reserved, Decimal(0))),
            "state": "exhausted" if row["exhausted_at"] else ("warning" if row["warned_at"] else "ok"),
            **{k: int(v) for k, v in calls.items()},
        }

    def set_cap(self, shop_id: str, cap: Decimal) -> None:
        month = month_of(self.clock())
        with self.db.tenant_tx(shop_id) as conn:
            conn.execute("UPDATE shops SET monthly_llm_cap_usd = %s WHERE id = %s", (cap, shop_id))
            self._ensure_month_row(conn, shop_id, month)
            conn.execute(
                """UPDATE shop_spend SET cap_usd = %s,
                          exhausted_at = CASE WHEN spent_usd + reserved_usd < %s THEN NULL ELSE exhausted_at END,
                          warned_at = CASE WHEN spent_usd + reserved_usd < %s * %s THEN NULL ELSE warned_at END
                   WHERE shop_id = %s AND month = %s""",
                (cap, cap, cap, self.warn_ratio, shop_id, month),
            )
            conn.execute(
                "INSERT INTO events (shop_id, type, detail) VALUES (%s, 'budget_cap_changed', %s)",
                (shop_id, Jsonb({"cap_usd": str(cap), "month": month.isoformat()})),
            )
