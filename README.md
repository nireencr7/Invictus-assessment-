# Riverbend work orders

A small service that turns messy customer messages ("my rear brake squeals and the gears skip on hills, can I bring it Saturday?") into structured, priced, **tenant-isolated** work orders for Riverbend Cycles' three shops.

The LLM reads; **code prices, dates and decides**:
- The model picks catalog services and quotes the customer's own words as evidence for each. It never sees a price.
- Postgres row-level security keeps each shop's customers invisible to the others, even when application code gets a query wrong.
- Duplicate deliveries, provider outages and the monthly spend cap are each handled, tested and demonstrated in [`docs/RESULTS.md`](docs/RESULTS.md).

## For reviewers: where each deliverable is

| The brief asks for | Here |
|---|---|
| Code, tests, one-command local run | this repo · **`make up`** (Docker) or **`make dev`** (no Docker) · `make test` |
| Architecture note, 2 pages max | [**`docs/ARCHITECTURE.pdf`**](docs/ARCHITECTURE.pdf) (source: [`ARCHITECTURE.md`](docs/ARCHITECTURE.md)) |
| Deployment path to GCP, IaC + plan | [`docs/DEPLOYMENT_GCP.md`](docs/DEPLOYMENT_GCP.md) · [`infra/terraform/`](infra/terraform) · [`deploy/cloudbuild.yaml`](deploy/cloudbuild.yaml) |
| Results table for the 40 samples | [`docs/RESULTS.md`](docs/RESULTS.md) (+ chaos, spend-cap and outage runs) |
| "Not built" list, next steps, 100 shops | [`docs/NOT_BUILT.md`](docs/NOT_BUILT.md) |
| Loom: one AI decision I overrode | _link_ · the incident is in [`docs/AI_JUDGEMENT.md`](docs/AI_JUDGEMENT.md), evidence in [`docs/loom-evidence/`](docs/loom-evidence/) |
| Tests for the risky parts, and why those | [`docs/TESTING.md`](docs/TESTING.md) |
| Also | [decision log](docs/DECISIONS.md) · [AI judgement](docs/AI_JUDGEMENT.md) · [clarifying questions & assumptions](docs/QUESTIONS.md) |

## Run it

```bash
make up      # Docker: Postgres 16 + migrations + API + 2 workers  ->  http://localhost:8000
```

No Docker? If Postgres binaries are installed (`initdb`, `pg_ctl`), use `make dev`. It starts a throwaway local Postgres plus the API with an embedded worker.

Then open **http://localhost:8000**:

1. **Load the 40 brief samples.** All 40 are posted, including the duplicate. Switch between shops; each sees only its own orders.
2. **Click any row** to see the conversation, every model call with its cost, and the timeline.
3. **Break the model provider → Outage** (`make dev` only), then send a message. It is acknowledged instantly, retried with backoff, then handed to staff with a **Retry AI** button.
4. **Set this shop's cap to 0.10** and send messages: they're *held*, not lost. Raise the cap to release them.

The provider defaults to an **offline stand-in**: hand-written outputs, $0, deterministic, clearly labelled as not a model. To use Claude, put `LLM_PROVIDER=anthropic` and `ANTHROPIC_API_KEY=…` in `.env`. Never commit it; `.env` is git-ignored.

Verified on Linux with Postgres 16: `make dev`, `make test` and `make eval`. The Docker path is built in CI; it wasn't run on the build machine (no Docker there).

### Or use the API directly

```bash
# Intake (as the email gateway). Repeat the same call: HTTP 200 + "duplicate": true, nothing new created.
curl -s localhost:8000/v1/shops/rb-northside/inbound-messages \
  -H 'X-Ingest-Key: dev-ingest-key' -H 'Content-Type: application/json' \
  -d '{"message_id":"m-1005","channel":"email","received_at":"2026-09-22T08:30:00",
       "body":"My rear brake squeals and the gears skip on hills, can I bring it Saturday?"}'

# A shop's open work orders, with estimates computed from the catalog
curl -s localhost:8000/v1/shops/rb-northside/work-orders -H 'Authorization: Bearer dev-northside-token'

# Another shop's token gets 404, not 403: we don't confirm the other shop's data exists
curl -s -o /dev/null -w '%{http_code}\n' localhost:8000/v1/shops/rb-northside/work-orders \
  -H 'Authorization: Bearer dev-lakeshore-token'
```

Other routes: `/v1/shops/{shop}/inbox`, `/conversations/{id}`, `/conversations/{id}/retry`, `/spend`, `/work-orders/{id}/status`; `/v1/customer/{shop}/{reply_token}` (what the customer sees); `PUT /v1/admin/shops/{shop}/budget`. OpenAPI is at `/docs`.

## Results, in one table

From [`docs/RESULTS.md`](docs/RESULTS.md), baseline run:

| Work orders | Follow-ups asked (both resumed) | Price quotes | Cancellations routed | Duplicates ignored | Failures → staff | Bad model outputs rejected | Model spend |
|---|---|---|---|---|---|---|---|
| 37 | 2 | 1 | 1 | 1 | 0 | 3 (all repaired) | **$0.75, simulated** |

> ⚠️ This table comes from the **offline stand-in**, not a model: no API key was available while building. It proves the plumbing, not the model. `make eval-live` reruns everything against Claude and rewrites `RESULTS.md` with real spend, roughly $1–3.

Under 30% injected provider failures, every outcome matches the baseline, with no duplicates. At the cap, messages are held, then released. In a full outage, all 39 messages are acknowledged, $0 is spent, every one is handed to staff, and all recover on retry.

## How it works

- **Intake** stores message + conversation + acknowledgement + job in one transaction and returns `202`. It never waits on the model.
- **Workers** claim jobs from Postgres (`SKIP LOCKED`, leases), reserve worst-case budget, call Claude with a strict JSON schema, validate shape *and* meaning, let a pure `policy` function decide, and commit the result with the job's completion.
- **Tenancy** is enforced three times: composite keys, row-level security, and per-shop credentials.
- See the [architecture note](docs/ARCHITECTURE.md) for the diagram, boundaries, data model and the three decisions most worth challenging.

## Configuration and secrets

All configuration goes through one typed class, [`src/riverbend/config.py`](src/riverbend/config.py) (pydantic-settings). It reads environment variables, or a local `.env` in development. Secrets are `SecretStr`, so they never print. [`.env.example`](.env.example) documents every variable. On GCP the same variables come from Cloud Run, with secrets mounted from Secret Manager. On Vertex AI there is **no model API key at all**: the worker's service account is the credential.

## Repository map

```
src/riverbend/
  api.py, intake.py        HTTP, auth, idempotent intake
  jobqueue.py, worker.py   Postgres queue: claim/lease/retry/park/dead-letter
  pipeline.py              one message end to end
  llm/                     provider port, Anthropic/Vertex adapter, offline stand-in, breaker, fault injection, pricing
  extraction/              model contract: schema, prompt, validation (incl. grounding), date resolution
  policy.py                pure decision function
  catalog.py, budget.py    money in cents; per-shop monthly spend cap
  customer_copy.py         everything a customer is told
  evaluation.py, report.py the 40-sample harness -> docs/RESULTS.md
migrations/                schema + row-level security (immutable once applied; checksum-guarded)
tests/                     tests against real Postgres
data/                      catalog, the 40 samples, golden labels, offline outputs, simulated replies
infra/terraform/, deploy/  GCP
```

## Time spent

_Fill in honestly before sending._

## How this was built

With an AI coding assistant (Claude Code). [`docs/AI_JUDGEMENT.md`](docs/AI_JUDGEMENT.md) covers where its output was trusted, where it wasn't, and how each was checked.
