# One-command local runs:
#   make up       — Docker: Postgres + migrations + API + 2 workers + staff console -> http://localhost:3000
#   make dev      — no Docker: local Postgres binaries + API with an embedded worker -> http://localhost:8000
#   make console  — the staff console in dev mode (run alongside `make dev`)        -> http://localhost:5173
.PHONY: up down dev console console-test snapshot test lint fmt eval eval-live eval-gemini fixtures clean help

help:
	@grep -E '^[a-z-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  %-10s %s\n", $$1, $$2}'

up: ## Run everything in Docker (Postgres, migrations, API, 2 workers, staff console on :3000)
	docker compose up --build

down: ## Stop Docker services and delete their data
	docker compose down -v

dev: ## Run locally without Docker (needs Postgres binaries); API + embedded worker on :8000
	uv sync --quiet
	scripts/devdb.sh start
	uv run python -m riverbend migrate
	EMBEDDED_WORKER=true uv run python -m riverbend api --port 8000

console: ## Run the staff console (React) against the local API; open http://localhost:5173
	cd ui && npm install --no-audit --no-fund && npm run dev

console-test: ## Type-check, unit-test and build the staff console
	cd ui && npm ci --no-audit --no-fund && npm run typecheck && npm test && npm run build

snapshot: ## Regenerate the console's read-only demo dataset from a real (offline) run
	scripts/devdb.sh start
	uv run python -m riverbend migrate
	LOG_LEVEL=WARNING uv run python -m riverbend snapshot

test: ## Run the test suite (starts a throwaway Postgres if TEST_DATABASE_ADMIN_URL is unset)
	uv run pytest

lint: ## Lint and format-check
	uv run ruff check src tests scripts
	uv run ruff format --check src tests scripts

fmt: ## Auto-format
	uv run ruff format src tests scripts
	uv run ruff check --fix src tests scripts

eval: ## Run the 40 samples + chaos/budget/outage scenarios offline ($0) -> docs/RESULTS.md
	scripts/devdb.sh start
	uv run python -m riverbend migrate
	LOG_LEVEL=WARNING uv run python -m riverbend eval

eval-live: ## Same, against the real model (needs ANTHROPIC_API_KEY in .env). Costs roughly $1-3.
	scripts/devdb.sh start
	uv run python -m riverbend migrate
	LLM_PROVIDER=anthropic LOG_LEVEL=WARNING uv run python -m riverbend eval

eval-gemini: ## The 40 samples against Gemini on Vertex AI (gcloud ADC, VERTEX_PROJECT set). About $0.50.
	scripts/devdb.sh start
	uv run python -m riverbend migrate
	LLM_PROVIDER=gemini LLM_MODEL=gemini-3.8-flash VERTEX_PROJECT=$${VERTEX_PROJECT:?set VERTEX_PROJECT} \
	  LOG_LEVEL=WARNING uv run python -m riverbend eval --scenarios baseline --out docs/RESULTS_GEMINI.md

fixtures: ## Rebuild offline outputs, golden labels and simulated replies from one table
	uv run python scripts/build_offline_fixtures.py

clean: ## Stop the dev database and remove local state
	-scripts/devdb.sh stop
	rm -rf .devdb .pytest_cache .ruff_cache
