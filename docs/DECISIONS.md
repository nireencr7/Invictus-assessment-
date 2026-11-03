# Decision log

Short records of the choices most likely to come up on the call. Each says what was chosen, what it costs, and what would change my mind. The top three are argued in the [architecture note](ARCHITECTURE.md).

### D1. Postgres is the queue
**Chosen:** a `jobs` table, claimed with `FOR UPDATE SKIP LOCKED` and a lease token. Enqueued in the same transaction as the idempotent message insert.
**Costs:** polling; always-on workers; no fan-out.
**Revisit:** beyond roughly 50 messages per second, or when a second consumer appears. Then move to Pub/Sub or Cloud Tasks and keep this table as the source of truth.

### D2. Shared schema, row-level security, composite keys
**Chosen:** one schema for all shops; `shop_id` in every PK/FK; RLS `FORCE`d; a transaction-local tenant setting; a runtime role without BYPASSRLS or DELETE.
**Costs:** a bug in one policy affects every tenant; superusers bypass RLS.
**Revisit:** when a tenant needs its own residency, encryption keys or restore.

### D3. The model describes; code decides
**Chosen:** no prices in the prompt and no money fields in the schema. The model picks codes, wheels and how many bikes; `catalog` prices them in integer cents. The model classifies the timing phrase; `dates.resolve` computes the calendar window. `policy` is a pure function that decides what happens next.
**Costs:** a bigger schema; a date taxonomy that can't express every phrase (those become "unspecified" and trigger a follow-up).

### D4. Every claim must quote the customer verbatim
**Chosen:** `evidence` on every service and issue must appear in the customer's own text, ignoring case and punctuation, with `...` allowed for elisions. This catches invented services, upsells, and injected instructions that leak into extractions.
**Costs:** rejects honest paraphrases and costs one repair call.
**Revisit:** loosen to fuzzy matching only if live evals show recall loss.

### D5. Two layers of output checking, one repair
**Chosen:** the provider's structured-output mode constrains the *shape*. Pydantic `strict`/`extra=forbid` plus `semantic_errors` check *meaning*. On failure the model gets one repair call that lists the validator's errors; a second failure goes to staff. Rejected output is kept in `llm_calls.raw_output` for debugging and never becomes a work order.

### D6. Retries live in exactly one place
**Chosen:** SDK retries are off (`max_retries=0`). The job queue retries with exponential backoff and full jitter. A circuit breaker defers jobs **without spending an attempt** while the provider is down. Hand-off to staff happens on **message age** (SLA) or on max attempts. Retries are visible, budgeted and bounded.
**Known gap:** the breaker is per process.

### D7. Budget: reserve the worst case, settle the actual, hold at the cap
**Chosen:** before each call, an atomic conditional `UPDATE` reserves `max_input_estimate × cache-write price + max_tokens × output price`. Afterwards the actual usage cost is booked. Unknown outcomes (timeouts, crashes) are booked at the full reservation. At the cap: **no call**. The message is held, visible to staff, the customer is already acknowledged, and an alert fires. Raising the cap releases held messages.
**Costs:** pessimism strands up to one reservation of cap; in the $0.20 demo, about $0.10.
**Not chosen:** degrading to a cheaper model at the cap (a silent quality change), or dropping messages.

### D8. Required fields: what's wrong, and when
**Chosen:** bike type is extracted but not required. The shop sees the bike at drop-off, and requiring it would roughly triple the follow-ups across the samples. At most one follow-up question; `CHECK (follow_ups_asked <= 1)` enforces that in the database. If something is still missing after the reply, the work order is created with a flag (missing date) or the conversation goes to staff (no identifiable problem).
**Also:** code, not the model, decides whether a question is justified. A model asking "what bike is it?" when nothing required is missing is overruled.

### D9. Wrong shop: ask, never forward
**Chosen:** for m-1009 ("Hi Lakeshore team!" sent to Downtown), the system asks the customer which shop they meant. Nothing ever moves to another tenant automatically, and Downtown can't look up the "visit last week" at Lakeshore — that is the isolation guarantee working as intended.

### D10. Cancellations are requests, matched by code
**Chosen:** the model only classifies intent. Code looks for open orders in the *same shop* with the same customer name from the last 14 days. Exactly one match → `cancel_requested` for staff to confirm. Zero or several → staff.

### D11. Model: Claude Opus 5.5 at low effort, chosen by config, gated by eval
**Chosen:** the most capable model as the starting default, with `LLM_MODEL` switchable. With a per-shop spend cap, the default is really a cost/quality decision. `make eval-live` with `LLM_MODEL=claude-haiku-5-5` (about 40× cheaper) answers it with the golden set. I have not made that call without data.

### D12. An offline stand-in for the model
**Chosen:** `LLM_PROVIDER=offline` replays hand-written outputs for the 40 samples. Three of them are deliberately broken. Anything else falls back to a keyword heuristic, so the system and demo run at $0 and deterministically. It is labelled as not-a-model everywhere it appears.

### D13. Cross-tenant requests return 404, not 403
We don't confirm that another shop's resource exists.

### D14. Applied migrations are immutable
`run_migrations` records a checksum per file and refuses to run if an applied file changes. This came from a real incident during the build (see [AI_JUDGEMENT.md](AI_JUDGEMENT.md)).

### D15. The staff console is a separate app behind its own proxy
**Chosen:** a static React build in its own nginx container and Cloud Run service. nginx proxies `/v1` to the API, so the browser talks to one origin. `config.json` is written from env at start-up (`CONSOLE_MODE=live|snapshot|auto`). A snapshot mode serves a read-only export of a real run.
**Why:** the console ships and rolls back without touching the API; no CORS; no API URL baked into the bundle; the demo works with no backend.
**Costs:** a second deployable, and a second image to keep patched.
**Not chosen:** serving it from the FastAPI process (couples releases), or a CDN bucket (would need CORS and a build-time API URL).

### D16. The processing deadline restarts when processing restarts
**Chosen:** the SLA hand-off ("give the message to staff if the AI hasn't finished in N minutes") is measured from `jobs.sla_started_at`, which resets when staff press Retry AI or a cap release requeues the message. It used to be measured from the message's arrival, so a retried message went straight back to staff after one transient failure. Found by using the console; fixed in migration `003` with a regression test.

### D17. Customers are an email address within one shop
**Chosen:** a `customers` table keyed by `(shop_id, email)`, filled in by intake from the sender address, with row-level security like every other tenant table. The same address writing to two shops is two customers. Staff can send a message *as* a customer from the console (`POST /v1/shops/{shop}/test-messages`, staff token, own shop only), which goes through the normal intake path and is labelled a test on the timeline. It is off in production unless `TEST_MESSAGES=true`.
**Why:** staff asked for one place to see everything that happened with a customer, and a way to try the workflow without an email gateway. A per-shop key keeps "staff never see another shop's customers" true by construction.
**Costs:** one person with two addresses is two customers until merging exists. Messages with no sender (the brief's samples, most web forms) have no customer.
**Not chosen:** a cross-shop customer record (it would leak which other shops a person uses), or giving the browser the ingest key (it can post for every shop).

### D18. A hand-off always tells the customer, and staff answer in the same thread
**Chosen:** every hand-off to staff sends the customer a short `handoff` note ("a member of staff will reply to you personally"), once per hand-off rather than once per message while they wait. Staff reply from the conversation panel (`POST …/conversations/{id}/reply`). The reply is a `staff_reply` in the same outbox, with an idempotency key from the console, and it hands the conversation back: to *work order created* if it has one, otherwise *answered*.
**Why:** testing the console showed the gap. A customer who asked "is it negotiable?" after their order was made got silence, and staff had no way to answer from the console. Once an order exists, questions about it stay with people: the model is not asked, so it can never negotiate a price or change a confirmed order.
**Costs:** delivery is still the outbox (no SMTP yet), and staff replies are signed by the shop, not by a named person, until real staff identity exists.


### D19. Gemini runs on Vertex while the project has no Claude quota
**Chosen:** a second adapter behind the same provider port ([`llm/gemini_provider.py`](../src/riverbend/llm/gemini_provider.py)), selected with `LLM_PROVIDER=gemini` and `LLM_MODEL=gemini-3.8-flash`. The live deployment runs it. Claude stays one variable away (`llm_provider = "vertex"`).
**Why:** in a new GCP project every Claude model on Vertex answers `429` (quota 0) until Google approves an increase, and the regional endpoints answer `404`. Gemini has default quota, so the choice was "a real model now" or "the stand-in until someone at Google approves a form". Everything after the model call is unchanged: the same JSON schema constrains the output, the same validator checks shape, catalog codes and verbatim evidence, the same policy prices from the catalog. The port is what made this a one-file change.
**Costs:** the prompt was written for Claude and is used as is; the golden-label eval ([RESULTS_GEMINI.md](RESULTS_GEMINI.md)) is the evidence that it holds on Gemini: every check agreed (39/39 outcomes, 37/37 service sets, 35/35 estimates, 37/37 date windows) with no prompt changes. Gemini is booked at its standard 2027 rate ($1.50 / $7.50 per million tokens). Google bills half that until 2026-12-31, so shown spend is about twice the invoice until then: the cap errs on the safe side.
**Not chosen:** keeping the stand-in (no real AI at all), or an Anthropic API key in Secret Manager (it works, `LLM_PROVIDER=anthropic`, but adds a long-lived secret this deployment was designed not to have).

### D20. Staff sign in with email and password; the session token is used like a shop token
**Chosen:** accounts with an email, a scrypt-hashed password and a list of shops ([`006_staff_accounts.sql`](../migrations/006_staff_accounts.sql), [`staff_auth.py`](../src/riverbend/staff_auth.py)). Signing in returns a personal session token (`rbs_…`, stored only as its SHA-256) that goes in the same `Authorization: Bearer` header as before, so every route and the tenant check are unchanged. Sessions expire after 12 hours; sign-out revokes at once; a password change signs out the person's other sessions; disabling an account or resetting its password signs them out everywhere. Five wrong passwords lock the account for 15 minutes, counted in the database so it holds across instances. An admin creates accounts; there is no self sign-up. Shops' static tokens keep working for scripts and tools, and staff actions now record who did them.
**Why:** nobody remembers a 40-character token, and a shared token can't be revoked for one person, never expires, and can't say who did what. Handing out the shop token after a password check would keep all three problems. The session is per person, so it can be.
**Costs:** we own password security: hashing, lockout, and timing (an unknown email is checked against a dummy hash so it takes as long as a wrong password). There's no password reset by email (no sender yet: an admin sets a new one), no MFA and no SSO. The account tables are global rather than tenant tables, because sign-in must find an account before it knows the shop; they hold no customer data, and every shop-scoped request still runs under RLS for its one shop. Expired sessions accumulate, because the app role has no `DELETE`; a periodic clean-up job with the owner role would prune them.
**Revisit:** for real staff at more shops, move identity to Identity Platform or IAP (password reset, MFA, Google sign-in) with the shops as a claim. The check stays in `staff_shop`, so only `staff_credential` changes.
**Not chosen:** returning the shop's static token on login (shared, never expires, can't be revoked per person); JWTs (can't be revoked before they expire without a lookup anyway); Identity Platform now (an OAuth consent screen, a domain and a second sign-in system to run for a three-shop demo).

### D21. CD runs in GitHub Actions and signs in to GCP without a key
**Chosen:** a `deploy` job in the same workflow as CI ([`ci.yml`](../.github/workflows/ci.yml)). It runs on `main` only after the tests pass: build, push, migrate and wait, then roll out the worker, the API and the console, then a smoke test. It authenticates with Workload Identity Federation ([`cicd.tf`](../infra/terraform/cicd.tf)): GitHub's OIDC token is exchanged for a short-lived token of a deploy-only service account, and GCP accepts only this repository's `main` branch. The Cloud Run resources ignore `image` in Terraform, so the pipeline owns what runs and `terraform apply` owns everything else.
**Why:** the code is on GitHub, so one pipeline both gates and ships: a red test means no deploy, with no second system to wire together. A JSON key in a GitHub secret is the usual shortcut, and it is a long-lived credential this deployment was built not to have. The earlier `deploy/cloudbuild.yaml` was never triggered, and its test step couldn't run (Postgres refuses `initdb` as root). It's removed rather than left to mislead.
**Costs:** images are built twice (once to test the Dockerfile, once to ship it). There is no staging environment: `main` goes straight to the live demo. The deploy account has `run.developer` on the whole project (fine while the project holds only Riverbend). A failed rollout halfway leaves the new migration applied and older code running, which is why migrations must stay backward-compatible for one release.
**Revisit:** before real shops depend on it: add a staging project with the same pipeline, and promote by image tag. Add a manual approval on the `production` environment, and roll out with Cloud Run traffic splitting (deploy with no traffic, smoke-test the new revision, then move traffic).
**Not chosen:** a Cloud Build trigger (it needs the GitHub app connected in the console, and splits the gate from the deploy); a service-account key in GitHub secrets.

### D22. Workers take turns between shops, and one worker process runs 1–3 threads
**Chosen:** claiming is fair across shops ([`jobqueue.claim`](../src/riverbend/jobqueue.py)). A free worker takes the oldest job of the shop with the fewest jobs running and, on a tie, of the shop it served longest ago (`jobs.claimed_at`, [`007_fair_claim.sql`](../migrations/007_fair_claim.sql)). One worker process runs a [`WorkerPool`](../src/riverbend/worker.py) of 1–3 threads: one per 25 jobs waiting, added at once, removed one at a time once the backlog has stayed lower for 60 seconds. Every thread is the same worker loop.
**Why:** the queue was one oldest-first line for every shop, so a burst at one shop held up the rest: with 40 messages from one shop ahead of it, a second shop's first message started about 4 minutes later. Now it starts at the next free worker, even with one worker. "Fewest running" alone wasn't enough: with one worker nothing is running when it picks, every shop ties, and the flood still went first; the test that found this is in [`test_worker_pool.py`](../tests/test_worker_pool.py). Threads rather than more instances, because the work is mostly waiting on the model: one instance makes three calls at once, and nothing outside has to watch the queue to scale it.
**Costs:** claims take turns under a Postgres advisory lock, a few milliseconds each, so every claim sees the one before it. Without it, workers that freed up at the same instant all chose the same shop: a live run with three threads served Downtown three times before Lakeshore's first job, and a test now covers it. At this volume the turn-taking is free; at thousands of claims a second it would be the bottleneck. Three threads spend the same total on the model, three times faster: the spend cap and the provider's per-minute quota still bound it. No shop priority yet, and no per-shop thread limit (a limit would leave workers idle while only one shop has work).
**Not chosen:** a worker per shop that takes other shops' jobs when idle (doesn't fit 2 workers and 3 shops, or 100 shops, and needs reassigning as workers come and go); scaling Cloud Run instances on queue depth (an external scaler calling the Cloud Run API, for what threads already do).
