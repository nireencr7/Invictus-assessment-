"""Risk: Staff at one shop see another shop's customers or orders.

These tests connect as the real runtime role and try to break isolation the ways a bug
would: forgetting a WHERE clause, forgetting to set the tenant, writing the wrong shop_id,
reusing a pooled connection, calling the API with another shop's token.
"""

import psycopg
import pytest
from fastapi.testclient import TestClient

from riverbend.api import create_app
from tests.conftest import as_app, offline_output, post_message


@pytest.fixture
def two_shops(make_services):
    from tests.conftest import FakeProvider

    fake = FakeProvider(default=lambda req: offline_output("m-1010#1"))
    s = make_services(provider=fake)
    post_message(s, "rb-lakeshore", "m-L1", "Need new chain, old one is rusty. Road bike. Wednesday.")
    post_message(s, "rb-downtown", "m-D1", "Need new chain, old one is rusty. Road bike. Wednesday.")
    s.worker().drain()
    return s


def test_runtime_role_is_not_privileged(db_urls):
    with psycopg.connect(db_urls.app) as conn:
        row = conn.execute(
            "SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user"
        ).fetchone()
    assert row == (False, False)


def test_no_tenant_set_means_no_rows(two_shops, db_urls):
    with psycopg.connect(db_urls.app) as conn:
        for table in (
            "work_orders",
            "inbound_messages",
            "conversations",
            "outbound_messages",
            "llm_calls",
            "events",
        ):
            assert conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0, table


def test_query_without_where_clause_sees_only_current_shop(two_shops, db_urls):
    db = as_app(db_urls)
    try:
        with db.tenant_tx("rb-lakeshore") as conn:
            shops = {r["shop_id"] for r in conn.execute("SELECT shop_id FROM work_orders").fetchall()}
            bodies = conn.execute("SELECT count(*) AS n FROM inbound_messages").fetchone()["n"]
        assert shops == {"rb-lakeshore"} and bodies == 1
    finally:
        db.close()


def test_cannot_write_into_another_shop(two_shops, db_urls):
    db = as_app(db_urls)
    try:
        with pytest.raises(psycopg.errors.InsufficientPrivilege), db.tenant_tx("rb-lakeshore") as conn:
            conn.execute("INSERT INTO events (shop_id, type) VALUES ('rb-downtown', 'sneaky')")
    finally:
        db.close()


def test_cannot_update_another_shops_rows(two_shops, db_urls, admin):
    db = as_app(db_urls)
    try:
        with db.tenant_tx("rb-lakeshore") as conn:
            n = conn.execute(
                "UPDATE work_orders SET status = 'cancelled' WHERE shop_id = 'rb-downtown'"
            ).rowcount
        assert n == 0
    finally:
        db.close()
    assert (
        admin.execute("SELECT status FROM work_orders WHERE shop_id = 'rb-downtown'").fetchone()["status"]
        == "open"
    )


def test_composite_foreign_keys_forbid_cross_shop_references(two_shops, admin):
    # Even with RLS bypassed (superuser), the data model refuses a Lakeshore line item on a Downtown order.
    wo = admin.execute("SELECT id FROM work_orders WHERE shop_id = 'rb-downtown'").fetchone()["id"]
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        admin.execute(
            """INSERT INTO work_order_lines (shop_id, work_order_id, line_no, service_code, service_name, wheel,
                   quantity, unit_price_cents, line_total_cents, requires_inspection)
               VALUES ('rb-lakeshore', %s, 9, 'FLAT_FIX', 'x', 'front', 1, 1500, 1500, false)""",
            (wo,),
        )


def test_tenant_setting_does_not_leak_through_the_pool(two_shops, db_urls):
    db = as_app(db_urls)  # small pool: the same physical connection is reused
    try:
        with db.tenant_tx("rb-lakeshore") as conn:
            assert conn.execute("SELECT count(*) AS n FROM work_orders").fetchone()["n"] == 1
        with db.system_tx() as conn:
            assert conn.execute("SELECT count(*) AS n FROM work_orders").fetchone()["n"] == 0
            assert conn.execute("SELECT current_setting('app.current_shop', true) AS s").fetchone()["s"] in (
                None,
                "",
            )
    finally:
        db.close()


def test_runtime_role_cannot_delete(two_shops, db_urls):
    db = as_app(db_urls)
    try:
        with pytest.raises(psycopg.errors.InsufficientPrivilege), db.tenant_tx("rb-lakeshore") as conn:
            conn.execute("DELETE FROM work_orders")
    finally:
        db.close()


def test_api_hides_other_shops_behind_404(two_shops):
    with TestClient(create_app(two_shops.settings, two_shops)) as client:
        lake = {"Authorization": "Bearer dev-lakeshore-token"}
        assert client.get("/v1/shops/rb-lakeshore/work-orders", headers=lake).json()["count"] == 1
        for path in (
            "/v1/shops/rb-downtown/work-orders",
            "/v1/shops/rb-downtown/inbox",
            "/v1/shops/rb-downtown/spend",
        ):
            assert client.get(path, headers=lake).status_code == 404, path
        assert client.get("/v1/shops/rb-lakeshore/work-orders").status_code == 401
        assert (
            client.get(
                "/v1/shops/rb-lakeshore/work-orders", headers={"Authorization": "Bearer nope"}
            ).status_code
            == 401
        )
        # A Downtown work order id, requested through Lakeshore's scope, does not exist.
        with two_shops.db.tenant_tx("rb-downtown") as conn:
            dt = str(conn.execute("SELECT id FROM work_orders").fetchone()["id"])
        assert client.get(f"/v1/shops/rb-lakeshore/work-orders/{dt}", headers=lake).status_code == 404


def test_shop_id_is_validated_before_reaching_sql(two_shops):
    with pytest.raises(ValueError), two_shops.db.tenant_tx("rb-downtown' OR '1'='1"):
        pass
