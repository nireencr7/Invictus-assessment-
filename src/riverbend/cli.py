"""Command line: `python -m riverbend <command>`."""

from __future__ import annotations

import argparse
import logging
import sys

import psycopg

from riverbend.catalog import Catalog
from riverbend.config import get_settings
from riverbend.db import run_migrations

PUBLIC_CONTACTS = {
    "rb-downtown": "downtown@riverbendcycles.example",
    "rb-northside": "northside@riverbendcycles.example",
    "rb-lakeshore": "lakeshore@riverbendcycles.example",
}


def seed(admin_dsn: str, catalog: Catalog, cap, timezone: str) -> None:
    with psycopg.connect(admin_dsn, autocommit=True) as conn:
        for shop in catalog.shops.values():
            conn.execute(
                """INSERT INTO shops (id, name, timezone, monthly_llm_cap_usd, public_contact)
                   VALUES (%s, %s, %s, %s, %s)
                   ON CONFLICT (id) DO UPDATE SET name = EXCLUDED.name, public_contact = EXCLUDED.public_contact""",
                (shop.id, shop.name, timezone, cap, PUBLIC_CONTACTS.get(shop.id, "")),
            )


def _serve_health(port: int) -> None:
    """Minimal liveness endpoint so the worker can run as a Cloud Run service."""
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class Health(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"ok")

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("0.0.0.0", port), Health)
    threading.Thread(target=server.serve_forever, daemon=True, name="health").start()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="riverbend")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("migrate", help="apply migrations and seed shops")
    api = sub.add_parser("api", help="run the HTTP API")
    api.add_argument("--host", default="0.0.0.0")
    api.add_argument("--port", type=int, default=8000)
    wk = sub.add_parser("worker", help="run a worker")
    wk.add_argument(
        "--health-port", type=int, default=None, help="serve GET /healthz (Cloud Run needs a port)"
    )
    ev = sub.add_parser("eval", help="run the 40 sample messages and write docs/RESULTS.md")
    ev.add_argument("--scenarios", default="baseline,chaos,budget,outage")
    ev.add_argument("--out", default="docs/RESULTS.md")
    args = parser.parse_args(argv)

    settings = get_settings()
    logging.basicConfig(level=settings.log_level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    if args.cmd == "migrate":
        applied = run_migrations(
            settings.database_admin_url.get_secret_value(), settings.app_db_password.get_secret_value()
        )
        seed(
            settings.database_admin_url.get_secret_value(),
            Catalog.load(settings.catalog_path),
            settings.default_monthly_cap_usd,
            settings.shop_timezone,
        )
        print(f"migrations applied: {applied or 'none pending'}; shops seeded")
        return 0
    if args.cmd == "api":
        import uvicorn

        from riverbend.api import create_app

        uvicorn.run(create_app(settings), host=args.host, port=args.port)
        return 0
    if args.cmd == "worker":
        from riverbend.services import build_services
        from riverbend.worker import install_signal_handlers

        services = build_services(settings)
        worker = services.worker()
        install_signal_handlers(worker)
        if args.health_port:
            _serve_health(args.health_port)
        worker.run_forever()
        services.close()
        return 0
    if args.cmd == "eval":
        from riverbend.evaluation import run_eval

        return run_eval(settings, scenarios=args.scenarios.split(","), out_path=args.out)
    return 1


if __name__ == "__main__":
    sys.exit(main())
