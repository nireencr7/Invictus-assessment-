-- The processing SLA ("hand to staff if the AI hasn't finished within N minutes") must be
-- measured from when the *current* processing cycle began, not from when the message first
-- arrived. Otherwise a message that staff re-queue hours later ("Retry AI") is handed straight
-- back to staff after a single transient failure. Found while testing the staff console.
--
-- A new migration rather than an edit to 001: applied migrations are immutable (see db.py).
ALTER TABLE jobs ADD COLUMN sla_started_at timestamptz NOT NULL DEFAULT now();
