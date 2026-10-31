"""Test infrastructure.

Tests run against real Postgres, because the risky parts (RLS, unique constraints,
SKIP LOCKED, the budget's conditional UPDATE) are database behaviour that a fake would
only pretend to have.

- If TEST_DATABASE_ADMIN_URL is set (CI), that server is used.
- Otherwise a throwaway cluster is started from local Postgres binaries (initdb/pg_ctl).
Each test gets its own database cloned from a migrated template (~50 ms).
"""

from __future__ import annotations

import glob
import json
import os
import shutil
import socket
import subprocess
import tempfile
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import psycopg
import pytest

from riverbend.catalog import Catalog
from riverbend.cli import seed
from riverbend.config import REPO_ROOT, Settings
from riverbend.db import Database, run_migrations
from riverbend.llm.base import LLMRequest, LLMResponse, Usage
from riverbend.services import Services, build_services

APP_PASSWORD = "riverbend_app"
TEMPLATE = "riverbend_template"


def _pg_bin() -> str | None:
    for d in [
        os.environ.get("PG_BIN"),
        *sorted(glob.glob("/usr/lib/postgresql/*/bin"), reverse=True),
        "/opt/homebrew/bin",
        "/usr/local/bin",
    ]:
        if d and os.path.exists(os.path.join(d, "initdb")):
            return d
    found = shutil.which("initdb")
    return os.path.dirname(found) if found else None


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _with_db(dsn: str, dbname: str) -> str:
    base, _, _ = dsn.rpartition("/")
    return f"{base}/{dbname}"


@pytest.fixture(scope="session")
def pg_server():
    url = os.environ.get("TEST_DATABASE_ADMIN_URL")
    if url:
        yield url
        return
    bin_dir = _pg_bin()
    if not bin_dir:
        pytest.skip("No Postgres available: set TEST_DATABASE_ADMIN_URL or install Postgres binaries")
    tmp = tempfile.mkdtemp(prefix="riverbend-pg-")
    data, port = os.path.join(tmp, "data"), _free_port()
    subprocess.run(
        [
            os.path.join(bin_dir, "initdb"),
            "-D",
            data,
            "-U",
            "postgres",
            "--auth=trust",
            "-E",
            "UTF8",
            "--no-instructions",
        ],
        check=True,
        capture_output=True,
    )
    opts = f"-p {port} -k {tmp} -c listen_addresses=127.0.0.1 -c fsync=off -c synchronous_commit=off -c full_page_writes=off"
    subprocess.run(
        [
            os.path.join(bin_dir, "pg_ctl"),
            "-D",
            data,
            "-o",
            opts,
            "-l",
            os.path.join(tmp, "log"),
            "-w",
            "start",
        ],
        check=True,
        capture_output=True,
    )
    try:
        yield f"postgresql://postgres@127.0.0.1:{port}/postgres"
    finally:
        subprocess.run(
            [os.path.join(bin_dir, "pg_ctl"), "-D", data, "-m", "immediate", "stop"], capture_output=True
        )
        shutil.rmtree(tmp, ignore_errors=True)


@pytest.fixture(scope="session")
def template_db(pg_server):
    with psycopg.connect(pg_server, autocommit=True) as conn:
        conn.execute(f"DROP DATABASE IF EXISTS {TEMPLATE}")
        conn.execute(f"CREATE DATABASE {TEMPLATE}")
    admin = _with_db(pg_server, TEMPLATE)
    run_migrations(admin, APP_PASSWORD)
    seed(admin, Catalog.load(REPO_ROOT / "data" / "catalog.json"), "5.00", "America/Chicago")
    return TEMPLATE


@dataclass
class DbUrls:
    admin: str
    app: str


@pytest.fixture
def db_urls(pg_server, template_db) -> DbUrls:
    name = f"t_{uuid.uuid4().hex[:12]}"
    with psycopg.connect(pg_server, autocommit=True) as conn:
        conn.execute(f"CREATE DATABASE {name} TEMPLATE {template_db}")
    admin = _with_db(pg_server, name)
    app = admin.replace("://postgres@", f"://riverbend_app:{APP_PASSWORD}@", 1).replace(
        "://postgres:postgres@", f"://riverbend_app:{APP_PASSWORD}@", 1
    )
    yield DbUrls(admin=admin, app=app)
    with psycopg.connect(pg_server, autocommit=True) as conn:
        conn.execute(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)")


def make_settings(urls: DbUrls, **overrides) -> Settings:
    base = dict(
        _env_file=None,
        database_url=urls.app,
        database_admin_url=urls.admin,
        llm_provider="offline",
        retry_base_seconds=0.01,
        retry_max_seconds=0.05,
        breaker_failure_threshold=1000,
        processing_sla_seconds=3600,
        job_lease_seconds=30,
    )
    base.update(overrides)
    return Settings(**base)


@pytest.fixture
def settings(db_urls) -> Settings:
    return make_settings(db_urls)


@pytest.fixture
def admin(db_urls):
    with psycopg.connect(db_urls.admin, autocommit=True, row_factory=psycopg.rows.dict_row) as conn:
        yield conn


@dataclass
class FakeProvider:
    """Programmable provider: `script` maps (conversation_key, turn) -> list of results per call.
    A result is a dict (returned as JSON), a str (returned raw) or an Exception (raised)."""

    script: dict = field(default_factory=dict)
    default: Callable[[LLMRequest], object] | None = None
    name: str = "fake"
    model: str = "claude-opus-5-5"
    calls: list = field(default_factory=list)

    def complete(self, request: LLMRequest) -> LLMResponse:
        key = (request.metadata.get("conversation_key"), request.metadata.get("turn"))
        self.calls.append((key, request))
        queue = self.script.get(key)
        if queue:
            item = queue.pop(0) if len(queue) > 1 else queue[0]
        elif self.default:
            item = self.default(request)
        else:
            raise AssertionError(f"FakeProvider has no script for {key}")
        if isinstance(item, Exception):
            raise item
        text = item if isinstance(item, str) else json.dumps(item)
        return LLMResponse(
            text=text,
            usage=Usage(input_tokens=2000, output_tokens=400),
            model=self.model,
            stop_reason="end_turn",
            latency_ms=3,
        )


@pytest.fixture
def make_services(db_urls):
    created: list[Services] = []

    def _make(provider=None, **overrides) -> Services:
        s = build_services(make_settings(db_urls, **overrides), provider=provider)
        created.append(s)
        return s

    yield _make
    for s in created:
        s.close()


def load_samples() -> list[dict]:
    return json.loads((REPO_ROOT / "data" / "samples.json").read_text())["messages"]


def offline_output(key: str, attempt: int = -1) -> dict:
    data = json.loads((REPO_ROOT / "data" / "offline_model_outputs.json").read_text())["outputs"]
    return data[key][attempt]


def payload(
    message_id: str, body: str, *, channel="email", received="2026-09-22T08:00:00", reply_token=None
) -> dict:
    p = {"message_id": message_id, "channel": channel, "received_at": received, "body": body}
    if reply_token:
        p["reply_token"] = reply_token
    return p


def post_message(services: Services, shop_id: str, message_id: str, body: str, **kw):
    from riverbend.intake import InboundPayload, ingest

    return ingest(services.db, shop_id, InboundPayload(**payload(message_id, body, **kw)))


def reply_token(admin, conversation_id: str) -> str:
    return admin.execute(
        "SELECT reply_token FROM conversations WHERE id = %s", (conversation_id,)
    ).fetchone()["reply_token"]


def as_app(urls: DbUrls) -> Database:
    return Database(urls.app, min_size=1, max_size=4)


__all__ = [
    "FakeProvider",
    "make_settings",
    "load_samples",
    "offline_output",
    "payload",
    "post_message",
    "reply_token",
    "as_app",
    "Path",
    "datetime",
]
