"""Processing one queued message, end to end.

    load (tenant tx) -> pre-checks -> budget reserve -> model call -> settle
        -> validate (+ one repair call) -> policy.decide -> apply + complete job (one tenant tx)

The model call happens *outside* any database transaction (it can take many seconds).
The only writes that matter — work order, outbound message, state change, job completion —
commit together at the end, and are idempotent, so at-least-once delivery is safe.
"""

from __future__ import annotations

import logging
import random
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import psycopg
from psycopg.types.json import Jsonb

from riverbend import customer_copy, jobqueue
from riverbend.budget import Budget
from riverbend.catalog import Catalog
from riverbend.config import Settings
from riverbend.db import Database
from riverbend.extraction import prompt as prompts
from riverbend.extraction.schema import Extraction, api_json_schema
from riverbend.extraction.validate import ValidationContext, parse_and_validate
from riverbend.jobqueue import Job
from riverbend.llm.base import (
    LLMProvider,
    LLMRequest,
    PermanentProviderError,
    ProviderRefusal,
    TransientProviderError,
)
from riverbend.llm.pricing import PRICES, cost_of, worst_case_cost
from riverbend.llm.resilience import CircuitBreaker
from riverbend.policy import (
    AskFollowUp,
    CreateWorkOrder,
    Decision,
    DecisionContext,
    Escalate,
    Redirect,
    RequestCancellation,
    SendQuote,
    decide,
)

log = logging.getLogger(__name__)

# Conversation states in which a new customer message goes straight to staff, no model call.
_STAFF_OWNED = {"work_order_created", "cancellation_requested", "redirected", "needs_staff", "closed"}


@dataclass
class Loaded:
    msg: dict
    conv: dict
    shop: dict
    turns: list[prompts.Turn]
    customer_texts: tuple[str, ...]
    conversation_key: str
    customer_turn: int


class Pipeline:
    def __init__(
        self,
        *,
        db: Database,
        catalog: Catalog,
        provider: LLMProvider,
        budget: Budget,
        breaker: CircuitBreaker,
        settings: Settings,
        clock=lambda: datetime.now(UTC),
        rng: random.Random | None = None,
    ):
        self.db = db
        self.catalog = catalog
        self.provider = provider
        self.budget = budget
        self.breaker = breaker
        self.settings = settings
        self.clock = clock
        self.rng = rng or random.Random()
        self.system_prompt = prompts.system_prompt(catalog)
        self.schema = api_json_schema(catalog.codes, list(catalog.shops))

    # ------------------------------------------------------------------ entry point
    def process(self, job: Job) -> str:
        try:
            return self._process(job)
        except jobqueue.LeaseLost:
            # Another worker re-claimed this job (our lease expired mid-call). Whatever we
            # were about to commit was rolled back; theirs is the result that counts.
            log.warning("job %s: lease lost; our results were discarded", job.id)
            return "lease_lost"

    def _process(self, job: Job) -> str:
        loaded = self._load(job)
        if isinstance(loaded, str):
            return loaded
        msg, conv = loaded.msg, loaded.conv

        if conv["state"] in _STAFF_OWNED and loaded.customer_turn > 1:
            return self._escalate(
                job,
                loaded,
                "customer_wrote_after_conversation_closed",
                "A new customer message arrived on a finished conversation.",
                dead=False,
            )

        # Measured from the start of this processing cycle (a staff retry or a budget release restarts it).
        age = self.clock() - job.sla_started_at
        if job.last_error and age > timedelta(seconds=self.settings.processing_sla_seconds):
            return self._escalate(
                job,
                loaded,
                "ai_unavailable",
                f"No AI result within {int(self.settings.processing_sla_seconds)}s: {job.last_error}",
                dead=True,
            )

        if not self.breaker.allow():
            wait = self.breaker.seconds_until_retry() + self.rng.uniform(0.0, 1.0)
            jobqueue.reschedule(self.db, job, wait, job.last_error or "circuit open", consume_attempt=False)
            return "deferred_breaker_open"

        request = LLMRequest(
            system=self.system_prompt,
            messages=[
                {
                    "role": "user",
                    "content": prompts.user_prompt(
                        job.shop_id, loaded.shop["name"], msg["channel"], loaded.turns
                    ),
                }
            ],
            json_schema=self.schema,
            max_tokens=self.settings.llm_max_tokens,
            metadata={
                "conversation_key": loaded.conversation_key,
                "turn": loaded.customer_turn,
                "shop_id": job.shop_id,
                "job_id": job.id,
            },
        )
        vctx = ValidationContext(self.catalog, job.shop_id, loaded.customer_texts)

        extraction: Extraction | None = None
        errors: tuple[str, ...] = ()
        for purpose in ("extract", "repair"):
            outcome = self._call_model(job, loaded, request, purpose, vctx)
            if isinstance(outcome, str):
                return outcome  # retry scheduled / held / escalated
            raw_text, result = outcome
            if isinstance(result, Extraction):
                extraction = result
                break
            errors = result.errors
            self._event(
                job.shop_id,
                conv["id"],
                msg["id"],
                "validation_failed",
                {"purpose": purpose, "errors": list(errors)},
            )
            request = LLMRequest(
                system=request.system,
                messages=[
                    *request.messages,
                    {"role": "assistant", "content": raw_text or "{}"},
                    {"role": "user", "content": prompts.repair_prompt(errors)},
                ],
                json_schema=request.json_schema,
                max_tokens=request.max_tokens,
                metadata=request.metadata,
            )
        if extraction is None:
            return self._escalate(
                job,
                loaded,
                "invalid_model_output",
                "Model output failed validation twice: " + "; ".join(errors[:5]),
                dead=True,
            )

        local_date = msg["received_at"].astimezone(ZoneInfo(loaded.shop["timezone"])).date()
        decision = decide(
            extraction,
            DecisionContext(
                shop_id=job.shop_id,
                shop_name=loaded.shop["name"],
                follow_ups_asked=conv["follow_ups_asked"],
                received_local_date=local_date,
                customer_texts=loaded.customer_texts,
            ),
            self.catalog,
        )
        return self._apply(job, loaded, extraction, decision)

    # ------------------------------------------------------------------ loading
    def _load(self, job: Job) -> Loaded | str:
        with self.db.tenant_tx(job.shop_id) as conn:
            msg = conn.execute("SELECT * FROM inbound_messages WHERE id = %s", (job.message_id,)).fetchone()
            if msg is None:
                jobqueue.complete(conn, job, status="dead", error="message not found")
                return "missing_message"
            if msg["status"] in ("processed", "needs_staff"):
                jobqueue.complete(conn, job)  # re-delivered after a crash: results already committed
                return "already_processed"
            conv = conn.execute(
                "SELECT * FROM conversations WHERE id = %s", (msg["conversation_id"],)
            ).fetchone()
            shop = conn.execute("SELECT * FROM shops WHERE id = %s", (job.shop_id,)).fetchone()
            inbound = conn.execute(
                """SELECT external_id, body, created_at, id FROM inbound_messages
                   WHERE conversation_id = %s AND created_at <= %s ORDER BY created_at, id""",
                (conv["id"], msg["created_at"]),
            ).fetchall()
            questions = conn.execute(
                "SELECT body, created_at FROM outbound_messages WHERE conversation_id = %s AND kind = 'follow_up' ORDER BY created_at",
                (conv["id"],),
            ).fetchall()
        timeline = [(m["created_at"], 0, prompts.Turn("customer", m["body"])) for m in inbound]
        timeline += [(q["created_at"], 1, prompts.Turn("shop", q["body"])) for q in questions]
        turns = [t for _, _, t in sorted(timeline, key=lambda x: (x[0], x[1]))]
        return Loaded(
            msg=msg,
            conv=conv,
            shop=shop,
            turns=turns,
            customer_texts=tuple(m["body"] for m in inbound),
            conversation_key=inbound[0]["external_id"],
            customer_turn=len(inbound),
        )

    # ------------------------------------------------------------------ model call
    def _call_model(
        self, job: Job, loaded: Loaded, request: LLMRequest, purpose: str, vctx: ValidationContext
    ):
        msg, conv = loaded.msg, loaded.conv
        model = self.provider.model
        prompt_chars = len(request.system) + sum(len(str(m["content"])) for m in request.messages)
        reservation = self.budget.reserve(
            shop_id=job.shop_id,
            message_id=msg["id"],
            job_id=job.id,
            purpose=purpose,
            provider=self.provider.name,
            model=model,
            amount=worst_case_cost(model, prompt_chars, request.max_tokens),
        )
        if reservation is None:
            return self._hold_for_budget(job, loaded)

        try:
            resp = self.provider.complete(request)
        except TransientProviderError as exc:
            self.budget.settle(
                reservation,
                status="timeout_unknown" if exc.billed_unknown else "transient_error",
                cost=None if exc.billed_unknown else Decimal(0),
                error=str(exc)[:500],
                request_id=exc.request_id,
            )
            opened = self.breaker.record_failure()
            self._event(
                job.shop_id,
                conv["id"],
                msg["id"],
                "llm_transient_error",
                {"error": str(exc)[:300], "attempt": job.attempts, "breaker_opened": opened},
            )
            return self._retry_or_escalate(job, loaded, str(exc))
        except ProviderRefusal as exc:
            self.budget.settle(
                reservation, status="refused", cost=None, error=str(exc), request_id=exc.request_id
            )
            return self._escalate(
                job, loaded, "model_refused", "The model declined to process this message.", dead=True
            )
        except PermanentProviderError as exc:
            log.error("permanent provider error (check configuration): %s", exc)
            self.budget.settle(
                reservation,
                status="permanent_error",
                cost=Decimal(0),
                error=str(exc)[:500],
                request_id=exc.request_id,
            )
            return self._escalate(job, loaded, "ai_configuration_error", str(exc)[:300], dead=True)

        self.breaker.record_success()
        pricing_model = resp.model if resp.model in PRICES else model
        cost = cost_of(pricing_model, resp.usage)
        result = parse_and_validate(resp.text, vctx)
        valid = isinstance(result, Extraction)
        self.budget.settle(
            reservation,
            status="ok" if valid else "invalid_output",
            cost=cost,
            usage=resp.usage,
            latency_ms=resp.latency_ms,
            request_id=resp.request_id,
            raw_output=None if valid else resp.text[:20000],
            error=None if valid else "; ".join(result.errors)[:1000],
        )
        self._event(
            job.shop_id,
            conv["id"],
            msg["id"],
            "llm_call",
            {
                "purpose": purpose,
                "valid": valid,
                "cost_usd": str(cost),
                "latency_ms": resp.latency_ms,
                "model": resp.model,
                "simulated": resp.simulated,
            },
        )
        return resp.text, result

    def _retry_or_escalate(self, job: Job, loaded: Loaded, error: str) -> str:
        if job.attempts >= self.settings.max_attempts:
            return self._escalate(
                job,
                loaded,
                "ai_unavailable",
                f"Gave up after {job.attempts} attempts: {error[:200]}",
                dead=True,
            )
        delay = jobqueue.backoff_seconds(
            job.attempts, self.settings.retry_base_seconds, self.settings.retry_max_seconds, self.rng
        )
        jobqueue.reschedule(self.db, job, delay, error[:500], consume_attempt=True)
        return "retry_scheduled"

    def _hold_for_budget(self, job: Job, loaded: Loaded) -> str:
        with self.db.tenant_tx(job.shop_id) as conn:
            conn.execute(
                "UPDATE inbound_messages SET status = 'held_budget', outcome = 'held_budget' WHERE id = %s",
                (loaded.msg["id"],),
            )
            conn.execute(
                "UPDATE conversations SET state_reason = 'held_budget', updated_at = now() WHERE id = %s",
                (loaded.conv["id"],),
            )
            self._event_tx(conn, job.shop_id, loaded.conv["id"], loaded.msg["id"], "held_budget", {})
            jobqueue.park(conn, job, "budget")
        return "held_budget"

    # ------------------------------------------------------------------ outcomes
    def _escalate(self, job: Job, loaded: Loaded, reason: str, detail: str, *, dead: bool) -> str:
        try:
            with self.db.tenant_tx(job.shop_id) as conn:
                self._mark_needs_staff(conn, loaded, reason, detail)
                jobqueue.complete(
                    conn, job, status="dead" if dead else "done", error=detail[:500] if dead else None
                )
        except jobqueue.LeaseLost:
            return "lease_lost"
        return f"escalated:{reason}"

    def _mark_needs_staff(self, conn, loaded: Loaded, reason: str, detail: str) -> None:
        conn.execute(
            "UPDATE inbound_messages SET status = 'needs_staff', outcome = %s WHERE id = %s",
            (reason, loaded.msg["id"]),
        )
        conn.execute(
            "UPDATE conversations SET state = 'needs_staff', state_reason = %s, updated_at = now() WHERE id = %s",
            (reason, loaded.conv["id"]),
        )
        self._event_tx(
            conn,
            loaded.msg["shop_id"],
            loaded.conv["id"],
            loaded.msg["id"],
            "escalated",
            {"reason": reason, "detail": detail},
        )

    def _apply(self, job: Job, loaded: Loaded, ex: Extraction, decision: Decision) -> str:
        msg, conv, shop = loaded.msg, loaded.conv, loaded.shop
        outcome: str
        try:
            with self.db.tenant_tx(job.shop_id) as conn:
                conn.execute(
                    "UPDATE conversations SET customer_name = COALESCE(NULLIF(%s, ''), customer_name) WHERE id = %s",
                    (ex.customer_name, conv["id"]),
                )
                if isinstance(decision, CreateWorkOrder):
                    outcome = self._create_work_order(conn, loaded, ex, decision)
                elif isinstance(decision, AskFollowUp):
                    conn.execute(
                        """UPDATE conversations SET state = 'awaiting_customer', follow_ups_asked = follow_ups_asked + 1,
                                  state_reason = %s, updated_at = now() WHERE id = %s""",
                        (decision.reason, conv["id"]),
                    )
                    self._send(
                        conn,
                        conv["id"],
                        "follow_up",
                        customer_copy.follow_up(decision.question),
                        f"follow_up:{conv['id']}",
                    )
                    self._event_tx(
                        conn,
                        job.shop_id,
                        conv["id"],
                        msg["id"],
                        "follow_up_asked",
                        {
                            "reason": decision.reason,
                            "question": decision.question,
                            "source": decision.question_source,
                        },
                    )
                    outcome = "follow_up_asked"
                elif isinstance(decision, SendQuote):
                    self._send(
                        conn,
                        conv["id"],
                        "price_quote",
                        customer_copy.price_quote(shop["name"], decision.lines, self.catalog),
                        f"quote:{msg['id']}",
                    )
                    conn.execute(
                        "UPDATE conversations SET state = 'answered', state_reason = 'price_quote', updated_at = now() WHERE id = %s",
                        (conv["id"],),
                    )
                    self._event_tx(
                        conn,
                        job.shop_id,
                        conv["id"],
                        msg["id"],
                        "price_quoted",
                        {"codes": [line.code for line in decision.lines]},
                    )
                    outcome = "price_quoted"
                elif isinstance(decision, RequestCancellation):
                    outcome = self._request_cancellation(conn, loaded, decision)
                elif isinstance(decision, Redirect):
                    other = self.catalog.shops[decision.target_shop_id]
                    contact = conn.execute(
                        "SELECT public_contact FROM shops WHERE id = %s", (other.id,)
                    ).fetchone()
                    self._send(
                        conn,
                        conv["id"],
                        "redirect",
                        customer_copy.redirect(
                            shop["name"], other.name, contact["public_contact"] if contact else ""
                        ),
                        f"redirect:{conv['id']}",
                    )
                    conn.execute(
                        "UPDATE conversations SET state = 'redirected', state_reason = %s, updated_at = now() WHERE id = %s",
                        (decision.target_shop_id, conv["id"]),
                    )
                    self._event_tx(
                        conn,
                        job.shop_id,
                        conv["id"],
                        msg["id"],
                        "redirected",
                        {"to": decision.target_shop_id},
                    )
                    outcome = "redirected"
                elif isinstance(decision, Escalate):
                    self._mark_needs_staff(conn, loaded, decision.reason, decision.detail)
                    outcome = f"escalated:{decision.reason}"
                else:  # pragma: no cover
                    raise TypeError(decision)

                if not outcome.startswith("escalated"):
                    conn.execute(
                        "UPDATE inbound_messages SET status = 'processed', outcome = %s WHERE id = %s",
                        (outcome, msg["id"]),
                    )
                jobqueue.complete(conn, job)
        except jobqueue.LeaseLost:
            log.warning(
                "job %s: lease lost before commit; results discarded (another worker owns it)", job.id
            )
            return "lease_lost"
        return outcome

    def _create_work_order(self, conn, loaded: Loaded, ex: Extraction, d: CreateWorkOrder) -> str:
        conv, msg, shop = loaded.conv, loaded.msg, loaded.shop
        w = d.window
        wo = conn.execute(
            """INSERT INTO work_orders
                 (shop_id, conversation_id, source_message_id, customer_name, bike_type, bike_count, brake_type,
                  issues, urgency, requested_earliest, requested_latest, requested_time_of_day, requested_time,
                  requested_phrase, estimate_cents, currency, estimate_is_provisional, review_reasons,
                  notes_for_staff, catalog_version, prompt_version, model, extraction)
               VALUES (current_setting('app.current_shop'), %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                       %s, %s, %s, %s, %s, %s, %s, %s, %s)
               ON CONFLICT (shop_id, conversation_id) DO NOTHING RETURNING id""",
            (
                conv["id"],
                msg["id"],
                ex.customer_name or None,
                ex.bike_type,
                ex.bike_count,
                ex.brake_type,
                Jsonb([i.model_dump() for i in ex.reported_issues]),
                d.urgency,
                w.earliest if w else None,
                w.latest if w else None,
                w.time_of_day if w else None,
                (w.time_24h or None) if w else None,
                ex.requested_date.phrase or None,
                d.estimate_cents,
                self.catalog.currency,
                any(line.requires_inspection for line in d.lines),
                list(d.review_reasons),
                ex.notes_for_staff or None,
                self.catalog.version,
                prompts.PROMPT_VERSION,
                self.provider.model,
                Jsonb(ex.model_dump()),
            ),
        ).fetchone()
        if wo is None:
            # A work order already exists for this conversation (re-processing after a crash). Idempotent no-op.
            self._event_tx(conn, msg["shop_id"], conv["id"], msg["id"], "work_order_exists", {})
            return "work_order_exists"
        for n, line in enumerate(d.lines, start=1):
            conn.execute(
                """INSERT INTO work_order_lines (shop_id, work_order_id, line_no, service_code, service_name, wheel,
                         quantity, unit_price_cents, line_total_cents, requires_inspection)
                   VALUES (current_setting('app.current_shop'), %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                (
                    wo["id"],
                    n,
                    line.code,
                    line.name,
                    line.wheel,
                    line.quantity,
                    line.unit_price_cents,
                    line.line_total_cents,
                    line.requires_inspection,
                ),
            )
        conn.execute(
            "UPDATE conversations SET state = 'work_order_created', state_reason = NULL, updated_at = now() WHERE id = %s",
            (conv["id"],),
        )
        self._send(
            conn,
            conv["id"],
            "summary",
            customer_copy.work_order_summary(
                shop["name"], ex.customer_name, d.lines, d.estimate_cents, d.window, self.catalog.currency
            ),
            f"summary:{wo['id']}",
        )
        self._event_tx(
            conn,
            msg["shop_id"],
            conv["id"],
            msg["id"],
            "work_order_created",
            {
                "work_order_id": str(wo["id"]),
                "estimate_cents": d.estimate_cents,
                "codes": [line.code for line in d.lines],
                "review": list(d.review_reasons),
            },
        )
        return "work_order_created"

    def _request_cancellation(self, conn, loaded: Loaded, d: RequestCancellation) -> str:
        conv, msg, shop = loaded.conv, loaded.msg, loaded.shop
        candidates = []
        if d.customer_name.strip():
            # Matching happens in code, inside this shop only (RLS), on facts we stored — not by the model.
            candidates = conn.execute(
                """SELECT id FROM work_orders
                   WHERE status = 'open' AND lower(customer_name) = lower(%s)
                     AND created_at > now() - interval '14 days'""",
                (d.customer_name.strip(),),
            ).fetchall()
        if len(candidates) != 1:
            detail = (
                f"Cancellation request from {d.customer_name or 'unknown customer'}: "
                f"{len(candidates)} matching open work orders; staff to confirm which."
            )
            self._mark_needs_staff(conn, loaded, "cancellation_needs_match", detail)
            return "escalated:cancellation_needs_match"
        conn.execute(
            "UPDATE work_orders SET status = 'cancel_requested', cancel_request_message_id = %s, updated_at = now() WHERE id = %s",
            (msg["id"], candidates[0]["id"]),
        )
        conn.execute(
            "UPDATE conversations SET state = 'cancellation_requested', updated_at = now() WHERE id = %s",
            (conv["id"],),
        )
        self._send(
            conn,
            conv["id"],
            "cancellation_ack",
            customer_copy.cancellation_ack(shop["name"]),
            f"cancel:{msg['id']}",
        )
        self._event_tx(
            conn,
            msg["shop_id"],
            conv["id"],
            msg["id"],
            "cancellation_requested",
            {"work_order_id": str(candidates[0]["id"])},
        )
        return "cancellation_requested"

    # ------------------------------------------------------------------ helpers
    @staticmethod
    def _send(conn, conversation_id, kind: str, body: str, dedupe_key: str) -> None:
        conn.execute(
            """INSERT INTO outbound_messages (shop_id, conversation_id, kind, body, dedupe_key)
               VALUES (current_setting('app.current_shop'), %s, %s, %s, %s)
               ON CONFLICT (shop_id, dedupe_key) DO NOTHING""",
            (conversation_id, kind, body, dedupe_key),
        )

    @staticmethod
    def _event_tx(conn, shop_id: str, conversation_id, message_id, type_: str, detail: dict) -> None:
        conn.execute(
            "INSERT INTO events (shop_id, conversation_id, message_id, type, detail) VALUES (%s, %s, %s, %s, %s)",
            (shop_id, conversation_id, message_id, type_, Jsonb(detail)),
        )

    def _event(self, shop_id: str, conversation_id, message_id, type_: str, detail: dict) -> None:
        try:
            with self.db.tenant_tx(shop_id) as conn:
                self._event_tx(conn, shop_id, conversation_id, message_id, type_, detail)
        except psycopg.Error:  # events are best-effort; never fail processing because of them
            log.exception("could not record event %s", type_)
