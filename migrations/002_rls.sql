-- Row-level security: the database itself enforces "staff at one shop never see another
-- shop's customers or orders".
--
-- Every tenant-scoped transaction runs `set_config('app.current_shop', <shop>, true)`
-- (transaction-local, so it cannot leak through the connection pool). With no shop set,
-- current_setting(..., true) is NULL and every policy matches zero rows: fail closed.
--
-- FORCE makes the policies apply to the table owner as well. The application connects as
-- `riverbend_app`, which is neither owner nor superuser and has no BYPASSRLS.
--
-- Not covered by RLS, deliberately:
--   shops  — reference data (names, caps); no customer data.
--   jobs   — ids and timestamps only; the worker must scan it across shops to claim work.

DO $$
DECLARE
    t text;
BEGIN
    FOREACH t IN ARRAY ARRAY[
        'conversations', 'inbound_messages', 'outbound_messages', 'work_orders',
        'work_order_lines', 'llm_calls', 'shop_spend', 'events'
    ] LOOP
        EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', t);
        EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY', t);
        EXECUTE format(
            'CREATE POLICY tenant_isolation ON %I
                 USING (shop_id = current_setting(''app.current_shop'', true))
                 WITH CHECK (shop_id = current_setting(''app.current_shop'', true))', t);
    END LOOP;
END $$;
