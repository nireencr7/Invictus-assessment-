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
| A UI to see it all | the **staff console** in [`ui/`](ui): a separate React app with its own container and Cloud Run service |
| Architecture note, 2 pages max | [**`docs/ARCHITECTURE.pdf`**](docs/ARCHITECTURE.pdf) (source: [`ARCHITECTURE.md`](docs/ARCHITECTURE.md)) |
| Deployment path to GCP, IaC + plan | [`docs/DEPLOYMENT_GCP.md`](docs/DEPLOYMENT_GCP.md) · [`infra/terraform/`](infra/terraform) · [`deploy/cloudbuild.yaml`](deploy/cloudbuild.yaml). **Deployed:** [staff console](https://riverbend-console-wndzwkly3a-uc.a.run.app) · [API](https://riverbend-api-wndzwkly3a-uc.a.run.app/docs) |
| Results table for the 40 samples | [`docs/RESULTS.md`](docs/RESULTS.md) (+ chaos, spend-cap and outage runs) |
| "Not built" list, next steps, 100 shops | [`docs/NOT_BUILT.md`](docs/NOT_BUILT.md) |
| Loom: one AI decision I overrode | _link_ · the incident is in [`docs/AI_JUDGEMENT.md`](docs/AI_JUDGEMENT.md), evidence in [`docs/loom-evidence/`](docs/loom-evidence/) |
| Tests for the risky parts, and why those | [`docs/TESTING.md`](docs/TESTING.md) |
| Also | [decision log](docs/DECISIONS.md) · [AI judgement](docs/AI_JUDGEMENT.md) · [clarifying questions & assumptions](docs/QUESTIONS.md) |

## Run it

```bash
make up      # Docker: Postgres 16 + migrations + API + 2 workers + staff console  ->  http://localhost:3000
```

No Docker? If Postgres binaries are installed (`initdb`, `pg_ctl`), run these two in separate terminals:

```bash
make dev        # throwaway local Postgres + API with an embedded worker on :8000
make console    # the staff console on http://localhost:5173 (proxies /v1 to :8000)
```

The provider defaults to an **offline stand-in**: hand-written outputs, $0, deterministic, clearly labelled as not a model. To use Claude, put `LLM_PROVIDER=anthropic` and `ANTHROPIC_API_KEY=…` in `.env`. Never commit it; `.env` is git-ignored.

Verified on Linux with Postgres 16: `make dev`, `make console`, `make test`, `make console-test` and `make eval`. The Docker images are proven by the GCP deployment (Cloud Build); `make up` itself wasn't run on the build machine, which has no Docker.

## The staff console

A separate app in [`ui/`](ui) (React + TypeScript + Vite). It is built into its own nginx container and deployed as its own Cloud Run service. It reaches the API only through its own `/v1` reverse proxy, so the browser sees one origin and the API needs no CORS.

- **Overview** of all shops: open orders, estimate totals, what needs a person, each shop's spend against its cap.
- **Work orders**, with filters (needs review, high urgency, cancel requested) and search.
- **Inbox**: handed to staff, held at the cap, waiting on the customer, answered.
- **Conversation drawer**:
  - the priced work-order ticket, with the customer's own words as evidence;
  - the full thread, and what the customer currently sees;
  - every model call with its cost, and the timeline;
  - actions: **Retry AI**, mark completed, cancel.
- **Activity** timeline.
- **Spend & model calls**: the cap meter, setting the cap, and every call, including rejected and failed ones.
- **Eval results**, checked against the golden labels.

Sign in with a staff token (each token opens one shop only), or press **Use local dev credentials**. On the GCP deployment, paste the `riverbend-staff-tokens` secret exactly as `gcloud` prints it to open all three shops.

Pages load when you open them; press **Refresh** in the top bar to see what the worker has done since. Auto-refresh is switched off for now. Uncommenting one line in `ui/src/App.tsx` turns it back on, every 15 seconds and only while the tab is visible.

**Snapshot mode.** The console also ships a read-only dataset exported from a real run (`make snapshot`), so it can be hosted with no backend at all (`CONSOLE_MODE=snapshot`). `CONSOLE_MODE=auto` falls back to the snapshot when the API doesn't answer.

**Using it caught a real bug.** Pressing *Retry AI* on an hour-old escalated message sent it straight back to staff after one transient failure. The processing deadline was measured from the message's arrival, not from the retry. It is fixed in migration `003`, with a regression test.

### Things to try

1. In the API's dev page (http://localhost:8000), press **Load the 40 brief samples**. In the console, switch shops: each sees only its own orders.
2. **Click any row** to open the conversation drawer.
3. On the dev page, **Break the model provider → Outage** (`make dev` only), then send a message from the console's Inbox. It is acknowledged instantly, retried with backoff, then handed to staff. Press **Retry AI** once the provider is healthy.
4. In **Spend**, set a shop's cap to 0.10 and send messages. They're *held*, not lost; raise the cap to release them.

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

Other routes: `/v1/me`, `/v1/shops/{shop}/inbox`, `/activity`, `/llm-calls`, `/conversations/{id}`, `/conversations/{id}/retry`, `/spend`, `/work-orders/{id}/status`; `/v1/customer/{shop}/{reply_token}` (what the customer sees); `PUT /v1/admin/shops/{shop}/budget`. OpenAPI is at `/docs`.

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
ui/                        the staff console: React app, nginx container, demo snapshot
infra/terraform/, deploy/  GCP
```

## Time spent

_Fill in honestly before sending._

## How this was built

With an AI coding assistant (Claude Code). [`docs/AI_JUDGEMENT.md`](docs/AI_JUDGEMENT.md) covers where its output was trusted, where it wasn't, and how each was checked.
