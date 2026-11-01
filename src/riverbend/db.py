"""Database access: connection pool plus the two kinds of transaction the app may open.

- `tenant_tx(shop_id)`: everything that touches customer data. Sets the RLS tenant for
  this transaction only. There is no API to read customer data without one.
- `system_tx()`: tenant-blind work on tables without customer data (claiming jobs).
  Under RLS it sees zero customer rows, by construction.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import psycopg
from psycopg import sql
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from riverbend.config import REPO_ROOT

MIGRATIONS_DIR = REPO_ROOT / "migrations"
_SHOP_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,62}$")


class Database:
    def __init__(self, dsn: str, min_size: int = 1, max_size: int = 10):
        self.pool = ConnectionPool(
            dsn,
            min_size=min_size,
            max_size=max_size,
            kwargs={"row_factory": dict_row, "autocommit": True},
            open=True,
        )

    def close(self) -> None:
        self.pool.close()

    @contextmanager
    def tenant_tx(self, shop_id: str) -> Iterator[psycopg.Connection]:
        if not _SHOP_ID_RE.match(shop_id):
            raise ValueError(f"invalid shop id {shop_id!r}")
        with self.pool.connection() as conn, conn.transaction():
            conn.execute("SELECT set_config('app.current_shop', %s, true)", (shop_id,))
            yield conn

    @contextmanager
    def system_tx(self) -> Iterator[psycopg.Connection]:
        with self.pool.connection() as conn, conn.transaction():
            yield conn


def run_migrations(admin_dsn: str, app_password: str, migrations_dir: Path = MIGRATIONS_DIR) -> list[str]:
    """Apply pending migrations and (re)assert the app role's privileges. Idempotent.

    Applied migrations are immutable. Each one's checksum is recorded; if a file that has
    already run is edited, we refuse to start instead of silently diverging (an edited
    migration fixes fresh databases and leaves every existing one broken). Fixes go in a
    new migration.
    """
    applied: list[str] = []
    with psycopg.connect(admin_dsn, autocommit=True) as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations (version text PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now())"
        )
        conn.execute("ALTER TABLE schema_migrations ADD COLUMN IF NOT EXISTS checksum text")
        done = {
            r[0]: r[1] for r in conn.execute("SELECT version, checksum FROM schema_migrations").fetchall()
        }
        for path in sorted(migrations_dir.glob("*.sql")):
            text = path.read_text()
            checksum = hashlib.sha256(text.encode()).hexdigest()
            if path.stem in done:
                if done[path.stem] is None:
                    # Applied before checksums were recorded: adopt the current file as the baseline.
                    conn.execute(
                        "UPDATE schema_migrations SET checksum = %s WHERE version = %s", (checksum, path.stem)
                    )
                elif done[path.stem] != checksum:
                    raise MigrationDrift(
                        f"migration {path.name} was edited after it was applied to this database. "
                        "Revert the edit and put the change in a new migration file."
                    )
                continue
            with conn.transaction():
                conn.execute(text)
                conn.execute(
                    "INSERT INTO schema_migrations (version, checksum) VALUES (%s, %s)", (path.stem, checksum)
                )
            applied.append(path.stem)
        ensure_app_role(conn, app_password)
    return applied


class MigrationDrift(RuntimeError):
    pass


def ensure_app_role(conn: psycopg.Connection, app_password: str) -> None:
    """The runtime role: no superuser, no BYPASSRLS, no DELETE anywhere."""
    exists = conn.execute("SELECT 1 FROM pg_roles WHERE rolname = 'riverbend_app'").fetchone()
    pw = sql.Literal(app_password)
    if exists:
        # Only LOGIN and the password: a non-superuser owner (Cloud SQL's) may not even *mention*
        # SUPERUSER or BYPASSRLS in ALTER ROLE. Whether they're off is checked below instead.
        conn.execute(sql.SQL("ALTER ROLE riverbend_app WITH LOGIN PASSWORD {}").format(pw))
    else:
        conn.execute(
            sql.SQL("CREATE ROLE riverbend_app WITH LOGIN NOSUPERUSER NOBYPASSRLS PASSWORD {}").format(pw)
        )
    su, bypass = conn.execute(
        "SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = 'riverbend_app'"
    ).fetchone()
    if su or bypass:
        raise RuntimeError(
            "riverbend_app must not be a superuser or bypass row-level security; fix the role by hand"
        )
    dbname = conn.execute("SELECT current_database()").fetchone()[0]
    conn.execute(sql.SQL("GRANT CONNECT ON DATABASE {} TO riverbend_app").format(sql.Identifier(dbname)))
    conn.execute("GRANT USAGE ON SCHEMA public TO riverbend_app")
    conn.execute("GRANT SELECT ON shops, schema_migrations TO riverbend_app")
    conn.execute("GRANT UPDATE (monthly_llm_cap_usd) ON shops TO riverbend_app")
    conn.execute(
        "GRANT SELECT, INSERT, UPDATE ON conversations, inbound_messages, work_orders, jobs, "
        "llm_calls, shop_spend, customers TO riverbend_app"
    )
    # Append-only from the app's point of view.
    conn.execute("GRANT SELECT, INSERT ON outbound_messages, work_order_lines, events TO riverbend_app")
    conn.execute("GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO riverbend_app")
