# Deploying to GCP

Infrastructure-as-code lives in [`infra/terraform/`](../infra/terraform). Every merge to `main` is tested and deployed by GitHub Actions ([`.github/workflows/ci.yml`](../.github/workflows/ci.yml), [below](#continuous-deployment)); first-time image builds use [`deploy/cloudbuild-images.yaml`](../deploy/cloudbuild-images.yaml).

## It is deployed

This Terraform has been applied for real, to a dedicated project created for it, with demo sizing (~$60–65/month while running).

| | |
|---|---|
| Project | `riverbend-wo-ac52e3` (us-central1) |
| Staff console | https://riverbend-console-wndzwkly3a-uc.a.run.app |
| API | https://riverbend-api-wndzwkly3a-uc.a.run.app (`/readyz`, `/docs`) |
| Model | **Gemini 3.8 Flash on Vertex AI** (`llm_provider = "gemini"`), a real model, called by the worker's service account. Claude on Vertex is wired up but the project has **zero Claude quota** until Google approves an increase; then set `llm_provider = "vertex"`, drop `llm_model`, and re-apply. See [D19](DECISIONS.md#d19-gemini-runs-on-vertex-while-the-project-has-no-claude-quota). |

**What was verified on the live deployment:**
- `terraform validate` against the provider schemas, then three `apply` runs (66 resources).
- Both images built by Cloud Build from the Dockerfiles; the console image runs its type-check and tests during the build.
- The migrations job applied `001`–`003` (schema, row-level security, the deadline fix) on Cloud SQL as a non-superuser owner.
- The 40 samples posted through the public API with the real ingest key: 39 accepted, 1 duplicate recognised.
- Every shop's work orders, inbox and spend read back with its real staff token, directly and through the console's proxy.
- Another shop's token gets `404`.
- Dev routes return `404` in production.
- The console serves its security headers and live config.
- **The real model, live** (`633b6d3`): a Spanish message sent through the public API ("los frenos … chirrían mucho y la cadena salta al cambiar … el jueves por la tarde") became a work order in one Gemini call (4 s, $0.008): brake adjustment on both wheels plus a gear adjustment, mountain bike, Thu 5 Nov afternoon, flagged `customer_language_es`. The keyword stand-in could not have read it. No warnings in the logs after the rollout.
- **A message that names no bike** (`2b161fa`): before, "Hi, what are the services you provide?" took two Gemini calls, because the first said `bike_count: 0` and was rejected. After the fix, "What are your opening hours on Saturday?" took one call ($0.0065), valid first time, and went to staff as not a repair request.

**Signing in to the console.** From the release with staff accounts ([D20](DECISIONS.md#d20-staff-sign-in-with-email-and-password-the-session-token-is-used-like-a-shop-token)) onwards, staff sign in with email and password. Nothing in Terraform changes: the tables come from migration `006`, run by the migrate job. There are no accounts until an admin creates them with the admin token (in `riverbend-admin-token`). See [the walkthrough](API_WALKTHROUGH.md#admin-staff) for the `curl`, or use **Create a staff account** in the Postman collection. The demo accounts from `make dev` never exist on GCP: they're created only when `DEV_MODE=true`, and the API runs with `DEV_MODE=false`.

Each shop's static token still works, under **Other ways to sign in**:
```bash
gcloud secrets versions access latest --secret=riverbend-staff-tokens --project riverbend-wo-ac52e3
```
That prints `{"<token>": "<shop>", …}`. Paste the output into the box as it is, and the console reads the tokens out of it. To open fewer shops, paste only those shops' tokens, one per line.

**What the first real deployment caught** (each is now fixed in this repo):
1. **Cloud Run rejects always-on CPU with under 512 MiB of memory.** When no CPU mode is set, the provider defaults to always-on, so the console (256 MiB) failed to create. Fix: `cpu_idle = true` set explicitly on the API and the console. It is also cheaper for services that scale to zero.
2. **Default service accounts got no permissions**, because this organisation enforces `iam.automaticIamGrantsForDefaultServiceAccounts`. Builds now run as a dedicated `riverbend-build` account with exactly three roles.
3. **No Claude quota in a new Vertex project** (HTTP 429 on every Claude model in `global`; the regional endpoints answer 404). The app's own outage handling would have sent every message to staff, so the deployment first ran the stand-in, and now runs Gemini on Vertex, which has default quota.
4. **The second migration run failed on Cloud SQL.** Re-asserting the app role with `ALTER ROLE riverbend_app … NOSUPERUSER NOBYPASSRLS` is refused there: the migration user isn't a superuser, and Postgres won't let a non-superuser even mention those flags. The first run had only *created* the role, and locally the owner is a superuser, so nothing caught it before. Migration `004` had committed; only the grants step after it failed. Fix: the role is now altered for login and password only, and the job checks `pg_roles` and fails loudly if the role can ever bypass row-level security.

5. **Slow Gemini calls end at Google, not in our client.** The SDK sends our 45 s timeout as `X-Server-Timeout`, so a slow call comes back as `504 DEADLINE_EXCEEDED` after ~42 s rather than as a client timeout. In the first real eval, 5 of 46 calls did this (median latency was 4.6 s); the queue retried each one and every message still got the right outcome. Fix: a 504 is now booked like a client timeout, at the reserved worst case, because the model may have run.

**Tear it all down** (deletion protection is off for this demo):
```bash
cd infra/terraform && terraform destroy     # then, to stop all billing for good:
gcloud projects delete riverbend-wo-ac52e3
```

## Services, and why these

| Need | Service | Why this one | Considered instead |
|---|---|---|---|
| Public HTTPS intake + staff API | **Cloud Run** service `riverbend-api` | Stateless, scales to zero, managed TLS, same image as everything else. It never calls the model, so it scales freely without spending. | GKE (more to operate than three shops need), App Engine |
| Staff console | **Cloud Run** service `riverbend-console` (nginx + the React build), scales to zero | Deployed and rolled back independently of the API. nginx proxies `/v1` to the API service, so the browser sees one origin: no CORS, no API URL baked into the build. `config.json` is written from env at start-up, so one image serves every environment. No secrets, no IAM roles. | Firebase Hosting / Cloud Storage + CDN (would then need CORS on the API), serving it from the API process (couples the deploys) |
| Queue consumers | **Cloud Run** service `riverbend-worker`, min = max = 2, **CPU always allocated**, internal ingress; each instance runs 1–3 worker threads, sized by the backlog ([D22](DECISIONS.md#d22-workers-take-turns-between-shops-and-one-worker-process-runs-13-threads)) | Pull-based workers need CPU between requests. A tiny `/healthz` lets them run as a normal service. Two instances give overlap during deploys, and SKIP LOCKED makes that safe. | Cloud Run *worker pools* (the better fit; adopt once in our provider version), a GCE managed instance group |
| Migrations | **Cloud Run job** `riverbend-migrate` | One-off, same image, executed by CI *before* traffic shifts. The checksum guard refuses edited migrations. | Running migrations on app start (races between instances) |
| Data, queue, spend ledger | **Cloud SQL for PostgreSQL 16** | RLS, `FOR UPDATE SKIP LOCKED`, transactional DDL, PITR backups, managed. Reached via the Cloud SQL connector (IAM + TLS, no authorised networks). | AlloyDB (later, for scale), Firestore (no RLS, no multi-row transactions of this shape) |
| The model | **Claude on Vertex AI** (live: **Gemini on Vertex AI** until Claude quota is granted) | **No API key exists anywhere**: only the worker's service account has `roles/aiplatform.user`, which covers both. Spend shows on the GCP invoice, quotas are per project, and traffic stays in Google's network. | Anthropic API key in Secret Manager (supported with `LLM_PROVIDER=anthropic`; it also unlocks server-side refusal fallback, which Vertex lacks) |
| Secrets and config | **Secret Manager** → env vars | Per-secret IAM: the API cannot read the owner DSN; only the migrate job can. Rotation means a new version, picked up at next instance start. | Env vars in Terraform state only |
| Images and delivery | **Artifact Registry**; **GitHub Actions** with Workload Identity Federation | The same workflow that tests every push ships `main`: tests → build → push → migrate → deploy, keyless ([below](#continuous-deployment)). Cloud Build is used only for the very first images. | A Cloud Build trigger (needs the GitHub app connected in the console; splits the test gate from the deploy) |
| Alerting | **Cloud Logging** metrics + **Cloud Monitoring** alerts | Alerts on: shop hit its cap, escalations because the AI is unavailable, invalid model output, permanent provider errors. | — |

The tenancy and failure boundaries carry over into IAM:
- only the worker can call the model;
- only the migrate job holds the owner credentials;
- the runtime DB role has no `BYPASSRLS` and no `DELETE`.

## Rollout

```bash
# 0. Once: project, billing, a private versioned bucket for Terraform state.
#    In the Vertex AI Model Garden, enable the Claude model for the project (terms + quota).
#    Until Claude quota is granted, llm_provider = "gemini" + llm_model = "gemini-3.8-flash" works out of the box.
cd infra/terraform
cp terraform.tfvars.example terraform.tfvars      # project_id, region, alert_email, image

# 1. The registry must exist before the first image.
terraform init -backend-config="bucket=<state-bucket>"
terraform apply -target=google_project_service.apis -target=google_artifact_registry_repository.app

# 2. First images: the API/worker image and the console image.
TAG=$(git rev-parse --short HEAD)
gcloud builds submit --tag "$REGION-docker.pkg.dev/$PROJECT/riverbend/app:$TAG" ../..
gcloud builds submit --tag "$REGION-docker.pkg.dev/$PROJECT/riverbend/console:$TAG" ../../ui

# 3. Everything else (set var.image and var.console_image to those tags).
terraform apply

# 4. Schema + shops, then smoke test.
gcloud run jobs execute riverbend-migrate --region "$REGION" --wait
curl "$(terraform output -raw api_url)/readyz"
open "$(terraform output -raw console_url)"          # sign in by pasting the riverbend-staff-tokens secret
```

## Continuous deployment

Every push to `main` runs [`.github/workflows/ci.yml`](../.github/workflows/ci.yml):

1. **test** (lint, the backend suite against Postgres 16, the API image builds) and **console** (type-check, tests, build, image) run in parallel.
2. Only if both pass, **deploy** runs:
   1. it builds both images, tagged with the commit;
   2. it pushes them to Artifact Registry;
   3. it **runs the migration job and waits for it**, so the schema is ready before any new code runs;
   4. it rolls out the worker, then the API, then the console;
   5. it smoke-tests `/readyz`, the console and sign-in.
3. Pull requests and other branches run the tests only. **Run workflow** in the Actions tab re-runs everything on `main`, deploy included.

**No key exists.** The job logs in with Workload Identity Federation ([`cicd.tf`](../infra/terraform/cicd.tf)):
- GitHub signs a short-lived OIDC token for the run.
- GCP trades it for a short-lived token of `riverbend-deploy`, but only if the token says it comes from this repository's `main` branch.
- `riverbend-deploy` may push images, update the Cloud Run services and the migration job, run that job, and act as the four runtime accounts. It can't read secrets or the database.

**Terraform and the pipeline don't fight.** Terraform owns the infrastructure, the pipeline owns which image runs, and the services' `lifecycle` blocks ignore the image. So a later `terraform apply` never rolls a deploy back. Infrastructure changes still go through `terraform plan`. `terraform.tfvars` still names the images used to *create* the services the first time.

**Setting it up once**, after `terraform apply` has created the pool and the account:

```bash
gh variable set GCP_PROJECT_ID --body "$PROJECT"
gh variable set GCP_WORKLOAD_IDENTITY_PROVIDER --body "$(terraform output -raw github_workload_identity_provider)"
gh variable set GCP_DEPLOY_SERVICE_ACCOUNT --body "$(terraform output -raw deploy_service_account)"
```

These are identifiers, not secrets. Until they exist the deploy job is skipped, so forks and fresh clones stay green.

## How an email reaches the API

The API doesn't read a mailbox. Something in front of it has to receive the email and post it, and that gateway isn't built here (see [NOT_BUILT.md](NOT_BUILT.md)). The usual shape:

1. **Each shop gets an inbound address**, e.g. `downtown@mail.riverbendcycles.example`. Its MX records point at an inbound-email service: SendGrid Inbound Parse, Mailgun Routes, Postmark Inbound or Amazon SES. Gmail or Google Workspace also works, with a watch that pushes to Pub/Sub.
2. **The service receives the email, parses it, and calls a webhook**, a small "email gateway" on Cloud Run. The gateway checks the webhook's signature, picks the shop from the `To` address, and takes the reply token from a `reply+<token>@` address if there is one.
3. **The gateway posts to the API** at `POST /v1/shops/{shop}/inbound-messages`, sending the `X-Ingest-Key` header and `message_id` (the email's Message-ID, so a redelivery is a duplicate, not a second request), `sender` (the From address), `channel`, `received_at`, `body` and the `reply_token`.
4. **The API stores the message, acknowledges it and queues it** in one transaction, then answers `202`. The worker does the AI part. Replies to the customer wait in the outbox (`outbound_messages`) for a sender to deliver them.

Until a gateway exists, there are two ways in: `scripts/load_samples.py` (posts with the ingest key, exactly like a gateway), and **Send a message as a customer** in the console (staff token, own shop only, switched on with `test_messages = true` in `terraform.tfvars`). The demo deployment has it on.

## Configuration flow

`src/riverbend/config.py` is the only reader of configuration. It is a typed `Settings` class, and secrets are `SecretStr` so they never appear in logs or tracebacks.
- **Locally:** `.env`, which is git-ignored. `.env.example` documents every variable.
- **On Cloud Run:** plain env vars from Terraform, plus secrets mounted from Secret Manager.

The code cannot tell the two apart. No key is committed anywhere. On GCP the model credential is the service account itself.

## Rough monthly cost (list prices, us-central1; check with the GCP calculator)

The live demo uses `db-f1-micro`, one worker and `api_max_instances = 3`: about **$60–65/month**, mostly the always-on worker. The table below is the production sizing.

| Item | Estimate | Note |
|---|---|---|
| Cloud SQL, 1 vCPU / 3.75 GB, zonal, 10 GB SSD + backups | ~$55 | HA (`REGIONAL`) roughly doubles it |
| 2 always-on workers, 1 vCPU / 512 MiB | ~$90–100 | The largest line. One worker at 0.5 vCPU is ~$25 and is enough for three shops. |
| API, console, Secret Manager, Artifact Registry, logging | < $10 | Both services scale to zero |
| Gemini 3.8 Flash on Vertex (live) | **~$0.008 per message, measured** (`make eval-gemini`: $0.35 for 41 messages at the 2027 rate, including one call booked at its worst case after Google's deadline) | ~1,000 messages a month ≈ $8, about half that until 2027. |
| Claude on Vertex (once quota is granted) | ~$0.02 per message on Opus 5.5 (simulated estimate; confirm with `make eval-live`) | ~1,000 messages a month ≈ $20. Haiku 5.5 would be ~40× cheaper if the eval holds up. |

For three shops the fixed cost (always-on workers + Cloud SQL) dominates, not the model. That is the main argument *against* decision 1 in the architecture note. A push model (Cloud Tasks → Cloud Run, scale to zero) would cut ~$80/month at the price of a second moving part.

## Production gaps to close before real customers

Staff routes and the console currently rely on per-shop bearer tokens. Put **Identity-Aware Proxy / Identity Platform** in front of the console and the API's staff routes, with the shop as a claim. Add **Cloud Armor** rate limits on intake. Move the database to a `REGIONAL` instance. Add an outbox **sender**: the outbox is written, but delivery is not built. All of these are in [NOT_BUILT.md](NOT_BUILT.md).
