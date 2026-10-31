"""Risk: an edited migration fixes fresh databases and silently leaves existing ones broken.
(This happened during development: see docs/AI_JUDGEMENT.md.)"""

import shutil

import psycopg
import pytest

from riverbend.db import MIGRATIONS_DIR, MigrationDrift, run_migrations
from tests.conftest import APP_PASSWORD


def test_migrations_are_idempotent(db_urls):
    assert run_migrations(db_urls.admin, APP_PASSWORD) == []


def test_editing_an_applied_migration_is_refused(db_urls, tmp_path):
    copy = tmp_path / "migrations"
    shutil.copytree(MIGRATIONS_DIR, copy)
    target = copy / "001_schema.sql"
    target.write_text(target.read_text() + "\n-- a quiet fix\n")
    with pytest.raises(MigrationDrift, match="001_schema.sql was edited"):
        run_migrations(db_urls.admin, APP_PASSWORD, copy)


def test_new_migration_is_applied(db_urls, tmp_path):
    copy = tmp_path / "migrations"
    shutil.copytree(MIGRATIONS_DIR, copy)
    (copy / "999_extra.sql").write_text("CREATE TABLE extra_thing (id int);")
    assert run_migrations(db_urls.admin, APP_PASSWORD, copy) == ["999_extra"]
    with psycopg.connect(db_urls.admin) as conn:
        assert (
            conn.execute("SELECT count(*) FROM schema_migrations WHERE checksum IS NOT NULL").fetchone()[0]
            == 3
        )


def test_databases_migrated_before_checksums_adopt_a_baseline(db_urls, tmp_path):
    with psycopg.connect(db_urls.admin, autocommit=True) as conn:
        conn.execute("UPDATE schema_migrations SET checksum = NULL")
    run_migrations(db_urls.admin, APP_PASSWORD)  # adopts current files
    copy = tmp_path / "migrations"
    shutil.copytree(MIGRATIONS_DIR, copy)
    (copy / "002_rls.sql").write_text((copy / "002_rls.sql").read_text() + "\n-- edit\n")
    with pytest.raises(MigrationDrift):
        run_migrations(db_urls.admin, APP_PASSWORD, copy)
