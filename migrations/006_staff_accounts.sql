-- Staff accounts: people sign in with an email address and password instead of pasting a shop token.
--
-- These are not tenant tables. Signing in has to find the account by email before anyone knows
-- which shop the request is for, and they hold no customer data. An account lists the shops it may
-- open; every shop-scoped request still runs in tenant_tx(shop) under row-level security, so an
-- account can never read a shop that isn't in its list. Shop ids are checked against the catalog
-- when an account is written (shops are configuration and are never deleted).
--
-- No password and no session token is ever stored: only a scrypt hash of the password and a
-- SHA-256 of the token. The runtime role still has no DELETE: signing out revokes a session.

CREATE TABLE staff_accounts (
    id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    email               text NOT NULL UNIQUE CHECK (email = lower(email) AND email ~ '^[^@\s<>]+@[^@\s<>]+$'),
    display_name        text CHECK (length(display_name) <= 200),
    password_hash       text NOT NULL CHECK (password_hash LIKE 'scrypt$%'),
    shops               text[] NOT NULL CHECK (cardinality(shops) BETWEEN 1 AND 100),
    disabled            boolean NOT NULL DEFAULT false,
    failed_logins       int NOT NULL DEFAULT 0 CHECK (failed_logins >= 0),
    locked_until        timestamptz,
    last_login_at       timestamptz,
    password_changed_at timestamptz NOT NULL DEFAULT now(),
    created_at          timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE staff_sessions (
    token_sha256 bytea PRIMARY KEY CHECK (length(token_sha256) = 32),
    account_id   uuid NOT NULL REFERENCES staff_accounts (id),
    created_at   timestamptz NOT NULL DEFAULT now(),
    expires_at   timestamptz NOT NULL,
    revoked_at   timestamptz,
    CHECK (expires_at > created_at)
);
CREATE INDEX staff_sessions_account_idx ON staff_sessions (account_id) WHERE revoked_at IS NULL;
