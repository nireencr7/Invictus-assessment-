-- Riverbend work orders: core schema.
--
-- Tenancy is part of the data model, not just the queries:
--   * every tenant-owned row carries shop_id NOT NULL;
--   * primary keys are (shop_id, id) and every foreign key is composite (shop_id, ...),
--     so a row in one shop physically cannot reference a row in another shop;
--   * 002_rls.sql adds row-level security so a query that forgets its WHERE clause
--     still only sees the current shop.

CREATE TABLE shops (
    id                   text PRIMARY KEY,
    name                 text NOT NULL,
    timezone             text NOT NULL DEFAULT 'America/Chicago',
    monthly_llm_cap_usd  numeric(10, 4) NOT NULL CHECK (monthly_llm_cap_usd >= 0),
    public_contact       text NOT NULL DEFAULT '',
    created_at           timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE conversations (
    shop_id           text NOT NULL REFERENCES shops (id),
    id                uuid NOT NULL DEFAULT gen_random_uuid(),
    channel           text NOT NULL CHECK (channel IN ('email', 'web')),
    -- Unguessable token carried in the reply-to address / web link. Lets a customer reply
    -- into this conversation without us trusting anything they typed.
    reply_token       text NOT NULL,
    state             text NOT NULL DEFAULT 'processing' CHECK (state IN (
                          'processing', 'awaiting_customer', 'work_order_created', 'answered',
                          'cancellation_requested', 'redirected', 'needs_staff', 'closed')),
    state_reason      text,
    -- "Ask one follow-up question" is enforced by the database, not just the code.
    follow_ups_asked  smallint NOT NULL DEFAULT 0 CHECK (follow_ups_asked BETWEEN 0 AND 1),
    customer_name     text,
    sender            text,
    created_at        timestamptz NOT NULL DEFAULT now(),
    updated_at        timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (shop_id, id),
    UNIQUE (shop_id, reply_token)
);

CREATE TABLE inbound_messages (
    shop_id               text NOT NULL REFERENCES shops (id),
    id                    uuid NOT NULL DEFAULT gen_random_uuid(),
    external_id           text NOT NULL,          -- message_id from the email gateway / web form
    conversation_id       uuid NOT NULL,
    channel               text NOT NULL CHECK (channel IN ('email', 'web')),
    sender                text,
    body                  text NOT NULL CHECK (length(body) BETWEEN 1 AND 10000),
    body_sha256           text NOT NULL,
    received_at           timestamptz NOT NULL,
    status                text NOT NULL DEFAULT 'queued'
                              CHECK (status IN ('queued', 'processed', 'held_budget', 'needs_staff')),
    outcome               text,
    duplicate_deliveries  integer NOT NULL DEFAULT 0,
    created_at            timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (shop_id, id),
    -- Idempotency: the same message delivered twice is one row.
    UNIQUE (shop_id, external_id),
    FOREIGN KEY (shop_id, conversation_id) REFERENCES conversations (shop_id, id)
);

-- Everything the customer is sent. Delivery (SMTP, web push) would drain this outbox;
-- the dedupe key makes "send" idempotent when a job is retried.
CREATE TABLE outbound_messages (
    shop_id          text NOT NULL REFERENCES shops (id),
    id               uuid NOT NULL DEFAULT gen_random_uuid(),
    conversation_id  uuid NOT NULL,
    kind             text NOT NULL CHECK (kind IN (
                         'ack', 'follow_up', 'summary', 'price_quote', 'cancellation_ack', 'redirect')),
    body             text NOT NULL,
    dedupe_key       text NOT NULL,
    created_at       timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (shop_id, id),
    UNIQUE (shop_id, dedupe_key),
    FOREIGN KEY (shop_id, conversation_id) REFERENCES conversations (shop_id, id)
);

-- The queue. Holds ids only (no customer text), so it is safe for the tenant-blind
-- worker to scan it. One job per inbound message, enforced by UNIQUE.
CREATE TABLE jobs (
    id            bigserial PRIMARY KEY,
    shop_id       text NOT NULL REFERENCES shops (id),
    message_id    uuid NOT NULL,
    status        text NOT NULL DEFAULT 'queued'
                      CHECK (status IN ('queued', 'running', 'done', 'parked', 'dead')),
    attempts      integer NOT NULL DEFAULT 0,
    run_after     timestamptz NOT NULL DEFAULT now(),
    lease_token   uuid,
    locked_until  timestamptz,
    park_reason   text,
    last_error    text,
    created_at    timestamptz NOT NULL DEFAULT now(),
    updated_at    timestamptz NOT NULL DEFAULT now(),
    UNIQUE (shop_id, message_id),
    FOREIGN KEY (shop_id, message_id) REFERENCES inbound_messages (shop_id, id)
);
CREATE INDEX jobs_ready_idx ON jobs (run_after, id) WHERE status IN ('queued', 'running');

CREATE TABLE work_orders (
    shop_id                    text NOT NULL REFERENCES shops (id),
    id                         uuid NOT NULL DEFAULT gen_random_uuid(),
    conversation_id            uuid NOT NULL,
    source_message_id          uuid NOT NULL,
    status                     text NOT NULL DEFAULT 'open'
                                   CHECK (status IN ('open', 'cancel_requested', 'cancelled', 'completed')),
    customer_name              text,
    bike_type                  text NOT NULL,
    bike_count                 smallint NOT NULL CHECK (bike_count BETWEEN 1 AND 5),
    brake_type                 text NOT NULL,
    issues                     jsonb NOT NULL,
    urgency                    text NOT NULL CHECK (urgency IN ('low', 'normal', 'high')),
    requested_earliest         date,
    requested_latest           date,
    requested_time_of_day      text,
    requested_time             text,
    requested_phrase           text,
    estimate_cents             integer NOT NULL CHECK (estimate_cents >= 0),
    currency                   text NOT NULL,
    estimate_is_provisional    boolean NOT NULL,
    review_reasons             text[] NOT NULL DEFAULT '{}',
    notes_for_staff            text,
    catalog_version            text NOT NULL,
    prompt_version             text NOT NULL,
    model                      text NOT NULL,
    extraction                 jsonb NOT NULL,   -- the validated model output, for audit
    cancel_request_message_id  uuid,
    created_at                 timestamptz NOT NULL DEFAULT now(),
    updated_at                 timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (shop_id, id),
    -- Second line of duplicate defence: one work order per conversation, whatever the queue does.
    UNIQUE (shop_id, conversation_id),
    CHECK (requested_earliest IS NULL OR requested_latest >= requested_earliest),
    FOREIGN KEY (shop_id, conversation_id) REFERENCES conversations (shop_id, id),
    FOREIGN KEY (shop_id, source_message_id) REFERENCES inbound_messages (shop_id, id)
);
CREATE INDEX work_orders_open_idx ON work_orders (shop_id, created_at) WHERE status IN ('open', 'cancel_requested');

CREATE TABLE work_order_lines (
    shop_id              text NOT NULL REFERENCES shops (id),
    work_order_id        uuid NOT NULL,
    line_no              smallint NOT NULL,
    service_code         text NOT NULL,
    service_name         text NOT NULL,
    wheel                text NOT NULL,
    quantity             smallint NOT NULL CHECK (quantity BETWEEN 1 AND 10),
    unit_price_cents     integer NOT NULL CHECK (unit_price_cents >= 0),
    line_total_cents     integer NOT NULL,
    requires_inspection  boolean NOT NULL,
    PRIMARY KEY (shop_id, work_order_id, line_no),
    CHECK (line_total_cents = unit_price_cents * quantity),
    FOREIGN KEY (shop_id, work_order_id) REFERENCES work_orders (shop_id, id)
);

-- The database refuses a work order whose stored estimate is not the sum of its lines.
CREATE FUNCTION check_work_order_total() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    wo_id uuid;
    stored bigint;
    summed bigint;
BEGIN
    -- Separate assignments: PL/pgSQL would resolve both arms of a CASE against NEW.
    IF TG_TABLE_NAME = 'work_orders' THEN
        wo_id := NEW.id;
    ELSE
        wo_id := NEW.work_order_id;
    END IF;
    SELECT estimate_cents INTO stored FROM work_orders WHERE shop_id = NEW.shop_id AND id = wo_id;
    SELECT COALESCE(SUM(line_total_cents), 0) INTO summed
      FROM work_order_lines WHERE shop_id = NEW.shop_id AND work_order_id = wo_id;
    IF stored IS DISTINCT FROM summed THEN
        RAISE EXCEPTION 'work order % estimate % does not equal sum of lines %', wo_id, stored, summed;
    END IF;
    RETURN NULL;
END $$;

CREATE CONSTRAINT TRIGGER work_order_total_matches AFTER INSERT OR UPDATE ON work_orders
    DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION check_work_order_total();
CREATE CONSTRAINT TRIGGER work_order_lines_total_matches AFTER INSERT OR UPDATE ON work_order_lines
    DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION check_work_order_total();

-- Every model call, including failed ones. Also the spend ledger's line items.
CREATE TABLE llm_calls (
    shop_id             text NOT NULL REFERENCES shops (id),
    id                  bigserial,
    message_id          uuid NOT NULL,
    job_id              bigint,
    purpose             text NOT NULL CHECK (purpose IN ('extract', 'repair')),
    provider            text NOT NULL,
    model               text NOT NULL,
    status              text NOT NULL CHECK (status IN (
                            'reserved', 'ok', 'invalid_output', 'transient_error', 'permanent_error',
                            'refused', 'timeout_unknown', 'expired')),
    month               date NOT NULL,
    reserved_usd        numeric(12, 6) NOT NULL CHECK (reserved_usd >= 0),
    cost_usd            numeric(12, 6),
    input_tokens        integer,
    output_tokens       integer,
    cache_read_tokens   integer,
    cache_write_tokens  integer,
    latency_ms          integer,
    request_id          text,
    error               text,
    raw_output          text,
    created_at          timestamptz NOT NULL DEFAULT now(),
    settled_at          timestamptz,
    PRIMARY KEY (shop_id, id),
    FOREIGN KEY (shop_id, message_id) REFERENCES inbound_messages (shop_id, id)
);
CREATE INDEX llm_calls_reserved_idx ON llm_calls (created_at) WHERE status = 'reserved';

-- Per-shop monthly budget. Reservations are taken *before* a call with a conditional
-- UPDATE, so concurrent workers cannot jointly overshoot the cap.
CREATE TABLE shop_spend (
    shop_id       text NOT NULL REFERENCES shops (id),
    month         date NOT NULL,
    cap_usd       numeric(10, 4) NOT NULL CHECK (cap_usd >= 0),
    spent_usd     numeric(12, 6) NOT NULL DEFAULT 0 CHECK (spent_usd >= 0),
    reserved_usd  numeric(12, 6) NOT NULL DEFAULT 0 CHECK (reserved_usd >= 0),
    warned_at     timestamptz,
    exhausted_at  timestamptz,
    PRIMARY KEY (shop_id, month)
);

-- Timeline for staff and for debugging ("what happened to this message?").
CREATE TABLE events (
    shop_id          text NOT NULL REFERENCES shops (id),
    id               bigserial,
    conversation_id  uuid,
    message_id       uuid,
    type             text NOT NULL,
    detail           jsonb NOT NULL DEFAULT '{}',
    created_at       timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (shop_id, id)
);
CREATE INDEX events_conversation_idx ON events (shop_id, conversation_id, id);
