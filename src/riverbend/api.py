"""HTTP surface.

Three kinds of caller, three credentials:
- the email gateway / web form backend   -> X-Ingest-Key (scoped to the shops it may post for)
- shop staff                             -> Bearer token bound to exactly one shop
- the customer                           -> the unguessable reply token in their link

Staff can also send a message as a customer of their own shop (the console's test tool), which
goes through the same intake as the gateway; it's off in production unless TEST_MESSAGES=true.

A staff token for shop A asking about shop B gets 404, not 403: we don't confirm that
another tenant's resources exist. The API process never calls the model.
"""

from __future__ import annotations

import hmac
import json
import logging
import threading
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Literal

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator

from riverbend import jobqueue
from riverbend.catalog import format_money
from riverbend.config import REPO_ROOT, Settings, get_settings
from riverbend.extraction.dates import DateWindow
from riverbend.intake import InboundPayload, ingest, parse_sender
from riverbend.llm.resilience import FaultInjectingProvider, FaultPlan
from riverbend.services import Services, build_services

log = logging.getLogger(__name__)
STATIC = Path(__file__).parent / "static"


class StatusChange(BaseModel):
    status: Literal["open", "cancelled", "completed"]


class CapChange(BaseModel):
    monthly_cap_usd: Decimal = Field(ge=0, le=1000)


class FaultChange(BaseModel):
    spec: str = Field(default="", max_length=100)


class TestMessage(BaseModel):
    """A message staff send as a customer of their own shop, to try the workflow end to end."""

    model_config = ConfigDict(extra="forbid")

    customer_email: str = Field(max_length=254)
    customer_name: str | None = Field(default=None, max_length=100)
    body: str = Field(min_length=1, max_length=10000)
    channel: Literal["email", "web"] = "email"
    reply_to: str | None = Field(
        default=None, max_length=64, description="Conversation id to reply in; omit for a new request."
    )

    @field_validator("customer_email")
    @classmethod
    def _email(cls, v: str) -> str:
        email, _ = parse_sender(v)
        if email is None or "<" in v:
            raise ValueError("customer_email must be a plain email address, like jamie.lee@example.com")
        return email

    @field_validator("customer_name")
    @classmethod
    def _name(cls, v: str | None) -> str | None:
        v = (v or "").replace('"', "").replace("\\", "").replace("<", "").replace(">", "").strip()
        return v or None

    @field_validator("body")
    @classmethod
    def _not_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("body must not be blank")
        return v.strip()


def _match(token: str | None, candidates: dict) -> str | None:
    if not token:
        return None
    for known, value in candidates.items():
        if hmac.compare_digest(known.encode(), token.encode()):
            return value
    return None


def create_app(settings: Settings | None = None, services: Services | None = None) -> FastAPI:
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        svc = services or build_services(settings)
        app.state.svc = svc
        worker_thread = None
        if settings.embedded_worker:
            worker = svc.worker()
            app.state.worker = worker
            worker_thread = threading.Thread(target=worker.run_forever, name="embedded-worker", daemon=True)
            worker_thread.start()
        yield
        if worker_thread:
            app.state.worker.stop()
            worker_thread.join(timeout=10)
        if services is None:
            svc.close()

    app = FastAPI(title="Riverbend work orders", version="0.1.0", lifespan=lifespan)
    staff_tokens = settings.staff_token_map()
    ingest_keys = settings.ingest_key_map()
    # The staff console normally reaches the API through its own reverse proxy (same origin).
    # CORS is only for a console hosted on a different origin, and is off unless configured.
    if settings.cors_origins.strip():
        app.add_middleware(
            CORSMiddleware,
            allow_origins=[o.strip() for o in settings.cors_origins.split(",") if o.strip()],
            allow_methods=["GET", "POST", "PUT"],
            allow_headers=["Authorization", "Content-Type", "X-Ingest-Key"],
        )

    def svc(request: Request) -> Services:
        return request.app.state.svc

    def known_shop(shop_id: str, s: Services) -> None:
        if shop_id not in s.catalog.shops:
            raise HTTPException(404, "not found")

    def staff_shop(shop_id: str, request: Request, authorization: str | None = Header(default=None)) -> str:
        token = authorization.removeprefix("Bearer ").strip() if authorization else None
        bound = _match(token, staff_tokens)
        if bound is None:
            raise HTTPException(401, "missing or invalid staff token", headers={"WWW-Authenticate": "Bearer"})
        if bound != shop_id:
            raise HTTPException(404, "not found")
        known_shop(shop_id, svc(request))
        return shop_id

    def require_admin(authorization: str | None = Header(default=None)) -> None:
        token = authorization.removeprefix("Bearer ").strip() if authorization else ""
        if not hmac.compare_digest(token.encode(), settings.admin_token.get_secret_value().encode()):
            raise HTTPException(401, "admin token required")

    # ------------------------------------------------------------------ health
    @app.get("/healthz")
    def healthz():
        return {"ok": True}

    @app.get("/readyz")
    def readyz(s: Services = Depends(svc)):
        with s.db.system_tx() as conn:
            conn.execute("SELECT 1")
        return {"ok": True, "provider": s.provider.name, "model": s.provider.model}

    # ------------------------------------------------------------------ intake
    @app.post("/v1/shops/{shop_id}/inbound-messages")
    def post_inbound(
        shop_id: str,
        payload: InboundPayload,
        request: Request,
        x_ingest_key: str | None = Header(default=None),
    ):
        allowed = _match(x_ingest_key, ingest_keys)
        if allowed is None:
            raise HTTPException(401, "missing or invalid ingest key")
        if shop_id not in allowed:
            raise HTTPException(403, "this ingest key may not post for that shop")
        s = svc(request)
        known_shop(shop_id, s)
        result = ingest(s.db, shop_id, payload)
        body = {
            "message_id": result.message_id,
            "conversation_id": result.conversation_id,
            "duplicate": result.duplicate,
            "detail": result.detail,
        }
        return JSONResponse(body, status_code=result.status_code)

    # ------------------------------------------------------------------ staff reads
    @app.get("/v1/shops/{shop_id}/work-orders")
    def list_work_orders(
        status: Literal["open", "all"] = "open",
        shop_id: str = Depends(staff_shop),
        s: Services = Depends(svc),
    ):
        statuses = (
            ("open", "cancel_requested")
            if status == "open"
            else ("open", "cancel_requested", "cancelled", "completed")
        )
        with s.db.tenant_tx(shop_id) as conn:
            orders = conn.execute(
                "SELECT * FROM work_orders WHERE status = ANY(%s) ORDER BY created_at, id", (list(statuses),)
            ).fetchall()
            lines = conn.execute(
                "SELECT * FROM work_order_lines WHERE work_order_id = ANY(%s) ORDER BY work_order_id, line_no",
                ([o["id"] for o in orders],),
            ).fetchall()
        by_order: dict = {}
        for line in lines:
            by_order.setdefault(line["work_order_id"], []).append(line)
        items = [_work_order_json(o, by_order.get(o["id"], [])) for o in orders]
        total = sum(i["estimate"]["cents"] for i in items if i["status"] == "open")
        return {
            "shop_id": shop_id,
            "count": len(items),
            "open_estimate_total": format_money(total),
            "work_orders": items,
        }

    @app.get("/v1/shops/{shop_id}/work-orders/{work_order_id}")
    def get_work_order(work_order_id: str, shop_id: str = Depends(staff_shop), s: Services = Depends(svc)):
        with s.db.tenant_tx(shop_id) as conn:
            o = conn.execute("SELECT * FROM work_orders WHERE id::text = %s", (work_order_id,)).fetchone()
            if o is None:
                raise HTTPException(404, "not found")
            lines = conn.execute(
                "SELECT * FROM work_order_lines WHERE work_order_id = %s ORDER BY line_no", (o["id"],)
            ).fetchall()
        return _work_order_json(o, lines)

    @app.post("/v1/shops/{shop_id}/work-orders/{work_order_id}/status")
    def set_work_order_status(
        work_order_id: str,
        change: StatusChange,
        shop_id: str = Depends(staff_shop),
        s: Services = Depends(svc),
    ):
        with s.db.tenant_tx(shop_id) as conn:
            row = conn.execute(
                "UPDATE work_orders SET status = %s, updated_at = now() WHERE id::text = %s "
                "RETURNING id, conversation_id",
                (change.status, work_order_id),
            ).fetchone()
            if row is None:
                raise HTTPException(404, "not found")
            conn.execute(
                "INSERT INTO events (shop_id, conversation_id, type, detail) VALUES (%s, %s, 'work_order_status', %s)",
                (
                    shop_id,
                    row["conversation_id"],
                    _jsonb({"work_order_id": work_order_id, "status": change.status}),
                ),
            )
        return {"id": work_order_id, "status": change.status}

    @app.get("/v1/shops/{shop_id}/inbox")
    def inbox(shop_id: str = Depends(staff_shop), s: Services = Depends(svc)):
        """Conversations that are not (yet) a work order: in flight, waiting on the customer,
        held at the budget cap, or handed to a human."""
        with s.db.tenant_tx(shop_id) as conn:
            rows = conn.execute(
                """SELECT c.id AS conversation_id, c.state, c.state_reason, c.customer_name, c.follow_ups_asked,
                          c.updated_at, m.external_id, m.body, m.status AS message_status, m.outcome, m.created_at
                   FROM conversations c
                   JOIN LATERAL (SELECT * FROM inbound_messages im WHERE im.conversation_id = c.id
                                 ORDER BY im.created_at DESC LIMIT 1) m ON true
                   WHERE c.state IN ('processing', 'awaiting_customer', 'needs_staff', 'answered')
                   ORDER BY c.updated_at DESC"""
            ).fetchall()
        return {
            "shop_id": shop_id,
            "count": len(rows),
            "items": [
                {
                    **r,
                    "conversation_id": str(r["conversation_id"]),
                    "needs_action": r["state"] == "needs_staff" or r["message_status"] == "held_budget",
                }
                for r in rows
            ],
        }

    @app.get("/v1/shops/{shop_id}/conversations/{conversation_id}")
    def conversation(conversation_id: str, shop_id: str = Depends(staff_shop), s: Services = Depends(svc)):
        with s.db.tenant_tx(shop_id) as conn:
            conv = conn.execute(
                "SELECT * FROM conversations WHERE id::text = %s", (conversation_id,)
            ).fetchone()
            if conv is None:
                raise HTTPException(404, "not found")
            inbound = conn.execute(
                "SELECT external_id, body, received_at, status, outcome, created_at FROM inbound_messages "
                "WHERE conversation_id = %s ORDER BY created_at",
                (conv["id"],),
            ).fetchall()
            outbound = conn.execute(
                "SELECT kind, body, created_at FROM outbound_messages WHERE conversation_id = %s "
                "ORDER BY created_at",
                (conv["id"],),
            ).fetchall()
            events = conn.execute(
                "SELECT type, detail, created_at FROM events WHERE conversation_id = %s ORDER BY id",
                (conv["id"],),
            ).fetchall()
            calls = conn.execute(
                """SELECT purpose, status, model, cost_usd, input_tokens, output_tokens, latency_ms, error, created_at
                   FROM llm_calls WHERE message_id IN (SELECT id FROM inbound_messages WHERE conversation_id = %s)
                   ORDER BY id""",
                (conv["id"],),
            ).fetchall()
        conv = {k: v for k, v in conv.items() if k != "reply_token"}
        return {
            "conversation": conv,
            "inbound": inbound,
            "outbound": outbound,
            "events": events,
            "llm_calls": [
                {**c, "cost_usd": str(c["cost_usd"]) if c["cost_usd"] is not None else None} for c in calls
            ],
        }

    @app.post("/v1/shops/{shop_id}/conversations/{conversation_id}/retry")
    def retry_ai(conversation_id: str, shop_id: str = Depends(staff_shop), s: Services = Depends(svc)):
        """Staff: 'try the AI again' after an outage or a fix."""
        with s.db.tenant_tx(shop_id) as conn:
            m = conn.execute(
                """SELECT id FROM inbound_messages WHERE conversation_id::text = %s
                   ORDER BY created_at DESC LIMIT 1""",
                (conversation_id,),
            ).fetchone()
            if m is None:
                raise HTTPException(404, "not found")
            if not jobqueue.requeue(conn, shop_id, m["id"]):
                raise HTTPException(409, "nothing to retry for this conversation")
            conn.execute(
                "UPDATE inbound_messages SET status = 'queued', outcome = NULL WHERE id = %s", (m["id"],)
            )
            conn.execute(
                "UPDATE conversations SET state = 'processing', state_reason = NULL WHERE id::text = %s",
                (conversation_id,),
            )
        return {"requeued": True}

    @app.get("/v1/me")
    def me(request: Request, authorization: str | None = Header(default=None)):
        """Which shop does this staff token belong to? Lets a console sign in with just a token."""
        token = authorization.removeprefix("Bearer ").strip() if authorization else None
        shop_id = _match(token, staff_tokens)
        if shop_id is None:
            raise HTTPException(401, "missing or invalid staff token", headers={"WWW-Authenticate": "Bearer"})
        shop = svc(request).catalog.shops[shop_id]
        return {
            "shop_id": shop.id,
            "shop_name": shop.name,
            "role": "staff",
            "features": {"test_messages": settings.test_messages_enabled},
        }

    # ------------------------------------------------------------------ customers
    @app.get("/v1/shops/{shop_id}/customers")
    def list_customers(shop_id: str = Depends(staff_shop), s: Services = Depends(svc)):
        """Everyone who has written to this shop from an email address, most recently active first."""
        with s.db.tenant_tx(shop_id) as conn:
            rows = conn.execute(
                _CUSTOMER_SUMMARY + " GROUP BY cu.shop_id, cu.id ORDER BY last_activity DESC"
            ).fetchall()
            anonymous = conn.execute(
                "SELECT count(*) AS n FROM conversations WHERE customer_id IS NULL"
            ).fetchone()["n"]
        return {
            "shop_id": shop_id,
            "count": len(rows),
            "conversations_without_email": anonymous,
            "customers": [_customer_json(r) for r in rows],
        }

    @app.get("/v1/shops/{shop_id}/customers/{customer_id}")
    def customer_history(customer_id: str, shop_id: str = Depends(staff_shop), s: Services = Depends(svc)):
        """One customer's whole history at this shop: every conversation, every message both ways,
        and every system event (model calls, rejections, questions, hand-offs, status changes)."""
        with s.db.tenant_tx(shop_id) as conn:
            cu = conn.execute(
                _CUSTOMER_SUMMARY + " WHERE cu.id::text = %s GROUP BY cu.shop_id, cu.id", (customer_id,)
            ).fetchone()
            if cu is None:
                raise HTTPException(404, "not found")
            convs = conn.execute(
                """SELECT c.id, c.channel, c.state, c.state_reason, c.follow_ups_asked, c.customer_name,
                          c.created_at, c.updated_at, w.id AS work_order_id, w.status AS work_order_status,
                          w.estimate_cents, w.currency,
                          (SELECT body FROM inbound_messages im WHERE im.conversation_id = c.id
                           ORDER BY im.created_at LIMIT 1) AS first_message
                   FROM conversations c LEFT JOIN work_orders w ON w.conversation_id = c.id
                   WHERE c.customer_id = %s ORDER BY c.created_at""",
                (cu["id"],),
            ).fetchall()
            ids = [c["id"] for c in convs]
            work_orders = [str(c["work_order_id"]) for c in convs if c["work_order_id"]]
            inbound = conn.execute(
                "SELECT conversation_id, external_id, channel, body, status, created_at FROM inbound_messages "
                "WHERE conversation_id = ANY(%s) ORDER BY created_at",
                (ids,),
            ).fetchall()
            outbound = conn.execute(
                "SELECT conversation_id, kind, body, created_at FROM outbound_messages "
                "WHERE conversation_id = ANY(%s) ORDER BY created_at",
                (ids,),
            ).fetchall()
            events = conn.execute(
                """SELECT e.conversation_id, e.type, e.detail, e.created_at, m.external_id
                   FROM events e LEFT JOIN inbound_messages m ON m.id = e.message_id
                   WHERE e.conversation_id = ANY(%s)
                      OR (e.conversation_id IS NULL AND e.detail->>'work_order_id' = ANY(%s))
                   ORDER BY e.id""",
                (ids, work_orders),
            ).fetchall()
            cost = conn.execute(
                "SELECT COALESCE(sum(cost_usd), 0) AS usd, count(*) AS calls FROM llm_calls WHERE message_id IN "
                "(SELECT id FROM inbound_messages WHERE conversation_id = ANY(%s))",
                (ids,),
            ).fetchone()

        def conv_id(v) -> str | None:
            return None if v is None else str(v)

        # Same-transaction rows share a timestamp; the customer's message comes first, then what
        # the system did about it, then what we sent back.
        rank = {"customer_message": 0, "event": 1, "shop_message": 2}
        timeline = sorted(
            [
                *(
                    {
                        "kind": "customer_message",
                        "at": m["created_at"],
                        "conversation_id": conv_id(m["conversation_id"]),
                        "external_id": m["external_id"],
                        "channel": m["channel"],
                        "body": m["body"],
                        "status": m["status"],
                    }
                    for m in inbound
                ),
                *(
                    {
                        "kind": "shop_message",
                        "at": o["created_at"],
                        "conversation_id": conv_id(o["conversation_id"]),
                        "message_kind": o["kind"],
                        "body": o["body"],
                    }
                    for o in outbound
                ),
                *(
                    {
                        "kind": "event",
                        "at": e["created_at"],
                        "conversation_id": conv_id(e["conversation_id"]),
                        "type": e["type"],
                        "detail": e["detail"],
                        "external_id": e["external_id"],
                    }
                    for e in events
                ),
            ],
            key=lambda x: (x["at"], rank[x["kind"]]),
        )
        return {
            "customer": _customer_json(cu),
            "model_calls": cost["calls"],
            "model_cost_usd": str(cost["usd"]),
            "conversations": [
                {
                    "id": str(c["id"]),
                    "channel": c["channel"],
                    "state": c["state"],
                    "state_reason": c["state_reason"],
                    "follow_ups_asked": c["follow_ups_asked"],
                    "customer_name": c["customer_name"],
                    "first_message": c["first_message"],
                    "created_at": c["created_at"],
                    "updated_at": c["updated_at"],
                    "work_order": None
                    if c["work_order_id"] is None
                    else {
                        "id": str(c["work_order_id"]),
                        "status": c["work_order_status"],
                        "estimate": format_money(c["estimate_cents"], c["currency"]),
                    },
                }
                for c in convs
            ],
            "timeline": timeline,
        }

    @app.post("/v1/shops/{shop_id}/test-messages")
    def send_test_message(msg: TestMessage, shop_id: str = Depends(staff_shop), s: Services = Depends(svc)):
        """Staff test tool: send a message as a customer of this shop. It takes exactly the path a real
        email takes (stored, acknowledged, queued for the AI); the timeline labels it a test.
        Off in production unless TEST_MESSAGES=true."""
        if not settings.test_messages_enabled:
            raise HTTPException(404, "not found")
        reply_token = None
        if msg.reply_to:
            with s.db.tenant_tx(shop_id) as conn:
                row = conn.execute(
                    "SELECT reply_token FROM conversations WHERE id::text = %s", (msg.reply_to,)
                ).fetchone()
            if row is None:
                raise HTTPException(404, "not found")
            reply_token = row["reply_token"]
        payload = InboundPayload(
            message_id=f"test-{uuid.uuid4().hex[:12]}",
            channel=msg.channel,
            received_at=datetime.now(UTC),
            body=msg.body,
            sender=f'"{msg.customer_name}" <{msg.customer_email}>'
            if msg.customer_name
            else msg.customer_email,
            reply_token=reply_token,
        )
        result = ingest(s.db, shop_id, payload, via="staff test")
        with s.db.tenant_tx(shop_id) as conn:
            row = conn.execute(
                "SELECT customer_id FROM conversations WHERE id = %s", (result.conversation_id,)
            ).fetchone()
        return JSONResponse(
            {
                "message_id": result.message_id,
                "external_id": payload.message_id,
                "conversation_id": result.conversation_id,
                "customer_id": None if row["customer_id"] is None else str(row["customer_id"]),
                "reply": reply_token is not None,
                "detail": result.detail,
            },
            status_code=result.status_code,
        )

    @app.get("/v1/shops/{shop_id}/llm-calls")
    def llm_calls(limit: int = 100, shop_id: str = Depends(staff_shop), s: Services = Depends(svc)):
        """Recent model calls for this shop, including failed and rejected ones."""
        with s.db.tenant_tx(shop_id) as conn:
            rows = conn.execute(
                """SELECT c.id, c.purpose, c.status, c.provider, c.model, c.reserved_usd, c.cost_usd,
                          c.input_tokens, c.output_tokens, c.latency_ms, c.error, c.created_at,
                          m.external_id, m.conversation_id
                   FROM llm_calls c JOIN inbound_messages m ON m.id = c.message_id
                   ORDER BY c.id DESC LIMIT %s""",
                (max(1, min(limit, 500)),),
            ).fetchall()
        return {
            "shop_id": shop_id,
            "items": [
                {
                    **r,
                    "conversation_id": str(r["conversation_id"]),
                    "reserved_usd": str(r["reserved_usd"]),
                    "cost_usd": None if r["cost_usd"] is None else str(r["cost_usd"]),
                }
                for r in rows
            ],
        }

    @app.get("/v1/shops/{shop_id}/activity")
    def activity(limit: int = 60, shop_id: str = Depends(staff_shop), s: Services = Depends(svc)):
        """The shop's recent timeline: received, asked, created, escalated, held, ..."""
        with s.db.tenant_tx(shop_id) as conn:
            rows = conn.execute(
                """SELECT e.id, e.type, e.detail, e.created_at, e.conversation_id, m.external_id
                   FROM events e LEFT JOIN inbound_messages m ON m.id = e.message_id
                   ORDER BY e.id DESC LIMIT %s""",
                (max(1, min(limit, 500)),),
            ).fetchall()
        return {
            "shop_id": shop_id,
            "items": [
                {**r, "conversation_id": None if r["conversation_id"] is None else str(r["conversation_id"])}
                for r in rows
            ],
        }

    @app.get("/v1/shops/{shop_id}/spend")
    def spend(shop_id: str = Depends(staff_shop), s: Services = Depends(svc)):
        return s.budget.status(shop_id)

    # ------------------------------------------------------------------ customer view
    @app.get("/v1/customer/{shop_id}/{reply_token}")
    def customer_view(shop_id: str, reply_token: str, s: Services = Depends(svc)):
        """What the customer sees: only messages we sent them, and a plain-language status."""
        known_shop(shop_id, s)
        with s.db.tenant_tx(shop_id) as conn:
            conv = conn.execute(
                "SELECT id, state FROM conversations WHERE reply_token = %s", (reply_token,)
            ).fetchone()
            if conv is None:
                raise HTTPException(404, "not found")
            out = conn.execute(
                "SELECT kind, body, created_at FROM outbound_messages WHERE conversation_id = %s "
                "ORDER BY created_at",
                (conv["id"],),
            ).fetchall()
        status = {
            "processing": "We're reviewing your request.",
            "awaiting_customer": "We've asked you a question — just reply to continue.",
            "work_order_created": "Your request is with the workshop team.",
            "answered": "We've answered your question.",
            "cancellation_requested": "Your cancellation is being processed.",
            "redirected": "Please contact the other shop directly.",
            "needs_staff": "A team member is reviewing your request personally.",
            "closed": "This conversation is closed.",
        }[conv["state"]]
        return {"status": status, "messages": out}

    # ------------------------------------------------------------------ admin
    @app.put("/v1/admin/shops/{shop_id}/budget", dependencies=[Depends(require_admin)])
    def set_budget(shop_id: str, change: CapChange, s: Services = Depends(svc)):
        known_shop(shop_id, s)
        s.budget.set_cap(shop_id, change.monthly_cap_usd)
        released = jobqueue.unpark(s.db, shop_id, "budget")
        if released:
            with s.db.tenant_tx(shop_id) as conn:
                conn.execute(
                    "UPDATE inbound_messages SET status = 'queued', outcome = NULL WHERE status = 'held_budget'"
                )
                conn.execute(
                    "UPDATE conversations SET state_reason = NULL WHERE state_reason = 'held_budget'"
                )
        return {**s.budget.status(shop_id), "released_held_messages": released}

    @app.get("/v1/admin/provider", dependencies=[Depends(require_admin)])
    def provider_status(s: Services = Depends(svc)):
        return {"provider": s.provider.name, "model": s.provider.model, "breaker": s.breaker.state}

    # ------------------------------------------------------------------ dev dashboard
    if settings.dev_mode:

        @app.get("/", response_class=HTMLResponse)
        def dashboard():
            return (STATIC / "dashboard.html").read_text()

        @app.get("/v1/dev/config")
        def dev_config(s: Services = Depends(svc)):
            """Dev only: lets the local dashboard act as staff and as the email gateway."""
            return {
                "shops": [{"id": sh.id, "name": sh.name} for sh in s.catalog.shops.values()],
                "staff_tokens": {v: k for k, v in staff_tokens.items()},
                "ingest_key": next(iter(ingest_keys), None),
                "admin_token": settings.admin_token.get_secret_value(),
                "provider": s.pipeline.provider.name,
                "model": s.pipeline.provider.model,
                "embedded_worker": settings.embedded_worker,
                "breaker": s.breaker.state,
            }

        @app.get("/v1/dev/samples")
        def dev_samples():
            return json.loads((REPO_ROOT / "data" / "samples.json").read_text())

        @app.put("/v1/dev/faults")
        def dev_faults(change: FaultChange, s: Services = Depends(svc)):
            """Dev only: break the provider on purpose (e.g. 'outage', 'flaky:0.5') to demo failure handling.
            Affects the worker embedded in this process; separate worker processes keep their own config."""
            base = getattr(s.pipeline.provider, "inner", s.pipeline.provider)
            plan = FaultPlan.parse(change.spec)
            s.pipeline.provider = FaultInjectingProvider(base, plan) if plan.active else base
            if not plan.active:
                s.breaker.record_success()
            return {"provider": s.pipeline.provider.name, "faults": change.spec, "breaker": s.breaker.state}

    return app


def _jsonb(v):
    from psycopg.types.json import Jsonb

    return Jsonb(v)


# One row per customer with their conversation and work-order counts. Callers add WHERE/GROUP BY.
# Joins need no shop_id: row-level security already limits every table to the current shop.
_CUSTOMER_SUMMARY = """
    SELECT cu.id, cu.email, cu.display_name, cu.first_seen_at,
           (SELECT c2.customer_name FROM conversations c2
             WHERE c2.customer_id = cu.id AND c2.customer_name IS NOT NULL
             ORDER BY c2.updated_at DESC LIMIT 1) AS extracted_name,
           count(c.id) AS conversations,
           count(c.id) FILTER (WHERE c.state = 'awaiting_customer') AS awaiting_reply,
           count(c.id) FILTER (WHERE c.state = 'needs_staff') AS needs_staff,
           count(w.id) FILTER (WHERE w.status IN ('open', 'cancel_requested')) AS open_orders,
           COALESCE(sum(w.estimate_cents) FILTER (WHERE w.status = 'open'), 0) AS open_estimate_cents,
           COALESCE(max(c.updated_at), cu.last_seen_at) AS last_activity
    FROM customers cu
    LEFT JOIN conversations c ON c.customer_id = cu.id
    LEFT JOIN work_orders w ON w.conversation_id = c.id
"""


def _customer_json(r: dict) -> dict:
    return {
        "id": str(r["id"]),
        "email": r["email"],
        # What they call themselves in their From header, else what the model read in their message.
        "name": r["display_name"] or r["extracted_name"],
        "first_seen_at": r["first_seen_at"],
        "last_activity": r["last_activity"],
        "conversations": r["conversations"],
        "awaiting_reply": r["awaiting_reply"],
        "needs_staff": r["needs_staff"],
        "open_orders": r["open_orders"],
        "open_estimate": format_money(int(r["open_estimate_cents"])),
    }


def _work_order_json(o: dict, lines: list[dict]) -> dict:
    # The estimate is recomputed here from priced lines; the database also guarantees it
    # equals the stored total (constraint trigger), so the two can never disagree.
    total = sum(line["line_total_cents"] for line in lines)
    window = None
    if o["requested_earliest"]:
        window = DateWindow(
            o["requested_earliest"],
            o["requested_latest"],
            o["requested_time_of_day"] or "any",
            o["requested_time"] or "",
            o["requested_phrase"] or "",
        )
    return {
        "id": str(o["id"]),
        "status": o["status"],
        "created_at": o["created_at"].isoformat(),
        "conversation_id": str(o["conversation_id"]),
        "customer_name": o["customer_name"],
        "bike": {"type": o["bike_type"], "count": o["bike_count"], "brakes": o["brake_type"]},
        "issues": o["issues"],
        "urgency": o["urgency"],
        "requested": None
        if window is None
        else {
            "earliest": window.earliest.isoformat(),
            "latest": window.latest.isoformat(),
            "time_of_day": window.time_of_day,
            "time": window.time_24h or None,
            "phrase": window.phrase or None,
            "label": window.label,
        },
        "lines": [
            {
                "code": line["service_code"],
                "name": line["service_name"],
                "wheel": line["wheel"],
                "quantity": line["quantity"],
                "unit_price": format_money(line["unit_price_cents"], o["currency"]),
                "line_total": format_money(line["line_total_cents"], o["currency"]),
                "requires_inspection": line["requires_inspection"],
            }
            for line in lines
        ],
        "estimate": {
            "cents": total,
            "formatted": format_money(total, o["currency"]),
            "currency": o["currency"],
            "provisional": o["estimate_is_provisional"],
        },
        "review_reasons": o["review_reasons"],
        "notes_for_staff": o["notes_for_staff"],
        "model": o["model"],
        "catalog_version": o["catalog_version"],
    }
