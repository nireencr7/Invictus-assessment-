"""Accepting inbound messages: idempotent, tenant-scoped, and never blocked on the model.

One transaction stores the message, opens (or continues) the conversation, queues the
acknowledgement and enqueues the processing job. Either all of it happens or none of it.
The HTTP response does not wait for the model, so a provider outage never makes intake
slow or failing.
"""

from __future__ import annotations

import hashlib
import re
import secrets
from dataclasses import dataclass
from datetime import datetime
from email.utils import parseaddr
from typing import Literal
from zoneinfo import ZoneInfo

from psycopg.types.json import Jsonb
from pydantic import BaseModel, ConfigDict, Field, field_validator

from riverbend import customer_copy, jobqueue
from riverbend.db import Database


class InboundPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    message_id: str = Field(min_length=1, max_length=200, description="Gateway's id; the idempotency key.")
    channel: Literal["email", "web"]
    received_at: datetime = Field(description="When the customer sent it. Naive timestamps are shop-local.")
    body: str = Field(min_length=1, max_length=10000)
    sender: str | None = Field(default=None, max_length=320)
    reply_token: str | None = Field(
        default=None, max_length=64, description="From our reply-to address/link."
    )

    @field_validator("body")
    @classmethod
    def _not_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("body must not be blank")
        return v


@dataclass(frozen=True)
class IngestResult:
    status_code: int  # 202 accepted, 200 duplicate, 409 conflicting duplicate
    message_id: str | None
    conversation_id: str | None
    duplicate: bool
    detail: str = ""


class _LostRace(Exception):
    pass


def _hash(body: str) -> str:
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


_EMAIL = re.compile(r"^[^@\s<>]+@[^@\s<>]+\.[^@\s<>]+$")


def parse_sender(sender: str | None) -> tuple[str | None, str | None]:
    """'Jamie Lee <Jamie@Example.com>' -> ('jamie@example.com', 'Jamie Lee'). No usable address -> (None, None)."""
    if not sender:
        return None, None
    name, addr = parseaddr(sender)
    addr = addr.strip().lower()
    if not _EMAIL.match(addr) or len(addr) > 254:
        return None, None
    return addr, (name.strip()[:200] or None)


def _customer_id(conn, sender: str | None):
    """The customer this sender is, created on first contact. None when there's no address."""
    email, name = parse_sender(sender)
    if email is None:
        return None
    return conn.execute(
        """INSERT INTO customers (shop_id, email, display_name)
           VALUES (current_setting('app.current_shop'), %s, %s)
           ON CONFLICT (shop_id, email) DO UPDATE
               SET last_seen_at = now(), display_name = COALESCE(EXCLUDED.display_name, customers.display_name)
           RETURNING id""",
        (email, name),
    ).fetchone()["id"]


def _existing(conn, external_id: str, body_hash: str) -> IngestResult | None:
    row = conn.execute(
        "SELECT id, conversation_id, body_sha256 FROM inbound_messages WHERE external_id = %s", (external_id,)
    ).fetchone()
    if row is None:
        return None
    if row["body_sha256"] != body_hash:
        return IngestResult(
            409,
            str(row["id"]),
            str(row["conversation_id"]),
            True,
            "message_id already used for a different message body",
        )
    conn.execute(
        "UPDATE inbound_messages SET duplicate_deliveries = duplicate_deliveries + 1 WHERE id = %s",
        (row["id"],),
    )
    conn.execute(
        "INSERT INTO events (shop_id, conversation_id, message_id, type, detail) "
        "VALUES (current_setting('app.current_shop'), %s, %s, 'duplicate_delivery', %s)",
        (row["conversation_id"], row["id"], Jsonb({"external_id": external_id})),
    )
    return IngestResult(200, str(row["id"]), str(row["conversation_id"]), True, "duplicate delivery ignored")


def ingest(db: Database, shop_id: str, payload: InboundPayload, *, via: str | None = None) -> IngestResult:
    """`via` labels where the message came from on the timeline when it isn't the email gateway
    (e.g. a test message staff sent from the console)."""
    body_hash = _hash(payload.body)
    try:
        return _ingest_once(db, shop_id, payload, body_hash, via)
    except _LostRace:
        # A concurrent delivery of the same message committed first. Ours rolled back.
        with db.tenant_tx(shop_id) as conn:
            result = _existing(conn, payload.message_id, body_hash)
        assert result is not None
        return result


def _ingest_once(
    db: Database, shop_id: str, payload: InboundPayload, body_hash: str, via: str | None
) -> IngestResult:
    with db.tenant_tx(shop_id) as conn:
        dup = _existing(conn, payload.message_id, body_hash)
        if dup:
            return dup
        shop = conn.execute("SELECT name, timezone FROM shops WHERE id = %s", (shop_id,)).fetchone()
        received_at = payload.received_at
        if received_at.tzinfo is None:
            received_at = received_at.replace(tzinfo=ZoneInfo(shop["timezone"]))

        customer_id = _customer_id(conn, payload.sender)
        conv = None
        if payload.reply_token:
            conv = conn.execute(
                "SELECT id, state FROM conversations WHERE reply_token = %s", (payload.reply_token,)
            ).fetchone()
        is_new_conversation = conv is None
        if is_new_conversation:
            conv = conn.execute(
                """INSERT INTO conversations (shop_id, channel, reply_token, sender, customer_id)
                   VALUES (current_setting('app.current_shop'), %s, %s, %s, %s) RETURNING id, state""",
                (payload.channel, secrets.token_urlsafe(16), payload.sender, customer_id),
            ).fetchone()
        elif customer_id is not None:
            # A reply can be the first time we learn who wrote (e.g. a web-form request answered by email).
            conn.execute(
                "UPDATE conversations SET customer_id = COALESCE(customer_id, %s) WHERE id = %s",
                (customer_id, conv["id"]),
            )

        msg = conn.execute(
            """INSERT INTO inbound_messages
                   (shop_id, external_id, conversation_id, channel, sender, body, body_sha256, received_at)
               VALUES (current_setting('app.current_shop'), %s, %s, %s, %s, %s, %s, %s)
               ON CONFLICT (shop_id, external_id) DO NOTHING RETURNING id""",
            (
                payload.message_id,
                conv["id"],
                payload.channel,
                payload.sender,
                payload.body,
                body_hash,
                received_at,
            ),
        ).fetchone()
        if msg is None:
            raise _LostRace()

        if is_new_conversation:
            conn.execute(
                """INSERT INTO outbound_messages (shop_id, conversation_id, kind, body, dedupe_key)
                   VALUES (current_setting('app.current_shop'), %s, 'ack', %s, %s)""",
                (conv["id"], customer_copy.ack(shop["name"]), f"ack:{msg['id']}"),
            )
        elif conv["state"] == "awaiting_customer":
            conn.execute(
                "UPDATE conversations SET state = 'processing', updated_at = now() WHERE id = %s",
                (conv["id"],),
            )
        jobqueue.enqueue(conn, shop_id, msg["id"])
        conn.execute(
            "INSERT INTO events (shop_id, conversation_id, message_id, type, detail) "
            "VALUES (current_setting('app.current_shop'), %s, %s, 'received', %s)",
            (
                conv["id"],
                msg["id"],
                Jsonb(
                    {
                        "external_id": payload.message_id,
                        "channel": payload.channel,
                        "reply": not is_new_conversation,
                        **({"via": via} if via else {}),
                    }
                ),
            ),
        )
        return IngestResult(202, str(msg["id"]), str(conv["id"]), False, "accepted")
