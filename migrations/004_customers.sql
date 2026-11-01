-- Customers, identified by email address within one shop.
--
-- The same address writing to two shops is two customers: each shop only ever sees its own.
-- Messages with no usable address (most web-form messages, the 40 brief samples) simply have
-- no customer; nothing else depends on one existing.

CREATE TABLE customers (
    shop_id        text NOT NULL REFERENCES shops (id),
    id             uuid NOT NULL DEFAULT gen_random_uuid(),
    email          text NOT NULL CHECK (email = lower(email) AND email ~ '^[^@\s<>]+@[^@\s<>]+$'),
    display_name   text CHECK (length(display_name) <= 200),
    first_seen_at  timestamptz NOT NULL DEFAULT now(),
    last_seen_at   timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (shop_id, id),
    UNIQUE (shop_id, email)
);

ALTER TABLE conversations ADD COLUMN customer_id uuid;
ALTER TABLE conversations
    ADD CONSTRAINT conversations_customer_fk FOREIGN KEY (shop_id, customer_id) REFERENCES customers (shop_id, id);
CREATE INDEX conversations_customer_idx ON conversations (shop_id, customer_id, created_at);

ALTER TABLE customers ENABLE ROW LEVEL SECURITY;
ALTER TABLE customers FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON customers
    USING (shop_id = current_setting('app.current_shop', true))
    WITH CHECK (shop_id = current_setting('app.current_shop', true));

-- Backfill: conversations that already carry a sender address ("Name <a@b.c>" or "a@b.c").
INSERT INTO customers (shop_id, email, first_seen_at, last_seen_at)
SELECT shop_id, lower(substring(sender from '[^<>\s]+@[^<>\s]+')), min(created_at), max(updated_at)
FROM conversations
WHERE substring(sender from '[^<>\s]+@[^<>\s]+') IS NOT NULL
GROUP BY 1, 2
ON CONFLICT DO NOTHING;

UPDATE conversations c SET customer_id = cu.id
FROM customers cu
WHERE cu.shop_id = c.shop_id AND cu.email = lower(substring(c.sender from '[^<>\s]+@[^<>\s]+'));
