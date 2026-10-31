# Deploying to GCP

Infrastructure-as-code lives in [`infra/terraform/`](../infra/terraform). The image pipeline is [`deploy/cloudbuild.yaml`](../deploy/cloudbuild.yaml). **Nothing here has been applied**: there were no credits. The Terraform is syntax-checked (HCL parses) but has not been through `terraform validate` against provider schemas or a real `apply`. Treat it as a reviewed plan, not a proven deployment.

## Services, and why these

| Need | Service | Why this one | Considered instead |
|---|---|---|---|
| Public HTTPS intake + staff API | **Cloud Run** service `riverbend-api` | Stateless, scales to zero, managed TLS, same image as everything else. It never calls the model, so it scales freely without spending. | GKE (more to operate than three shops need), App Engine |
| Staff console | **Cloud Run** service `riverbend-console` (nginx + the React build), scales to zero | Deployed and rolled back independently of the API. nginx proxies `/v1` to the API service, so the browser sees one origin: no CORS, no API URL baked into the build. `config.json` is written from env at start-up, so one image serves every environment. No secrets, no IAM roles. | Firebase Hosting / Cloud Storage + CDN (would then need CORS on the API), serving it from the API process (couples the deploys) |
| Queue consumers | **Cloud Run** service `riverbend-worker`, min = max = 2, **CPU always allocated**, internal ingress | Pull-based workers need CPU between requests. A tiny `/healthz` lets them run as a normal service. Two instances give overlap during deploys, and SKIP LOCKED makes that safe. | Cloud Run *worker pools* (the better fit; adopt once in our provider version), a GCE managed instance group |
| Migrations | **Cloud Run job** `riverbend-migrate` | One-off, same image, executed by CI *before* traffic shifts. The checksum guard refuses edited migrations. | Running migrations on app start (races between instances) |
| Data, queue, spend ledger | **Cloud SQL for PostgreSQL 16** | RLS, `FOR UPDATE SKIP LOCKED`, transactional DDL, PITR backups, managed. Reached via the Cloud SQL connector (IAM + TLS, no authorised networks). | AlloyDB (later, for scale), Firestore (no RLS, no multi-row transactions of this shape) |
| The model | **Claude on Vertex AI** | **No API key exists anywhere**: only the worker's service account has `roles/aiplatform.user`. Spend shows on the GCP invoice, quotas are per project, and traffic stays in Google's network. | Anthropic API key in Secret Manager (supported with `LLM_PROVIDER=anthropic`; it also unlocks server-side refusal fallback, which Vertex lacks) |
| Secrets and config | **Secret Manager** → env vars | Per-secret IAM: the API cannot read the owner DSN; only the migrate job can. Rotation means a new version, picked up at next instance start. | Env vars in Terraform state only |
| Images | **Artifact Registry** + **Cloud Build** | Native and IAM-integrated. The pipeline runs tests → build → push → migrate → deploy. | GitHub Actions with Workload Identity Federation (equally good) |
| Alerting | **Cloud Logging** metrics + **Cloud Monitoring** alerts | Alerts on: shop hit its cap, escalations because the AI is unavailable, invalid model output, permanent provider errors. | — |

The tenancy and failure boundaries carry over into IAM:
- only the worker can call the model;
- only the migrate job holds the owner credentials;
- the runtime DB role has no `BYPASSRLS` and no `DELETE`.

## Rollout

```bash
# 0. Once: project, billing, a private versioned bucket for Terraform state.
#    In the Vertex AI Model Garden, enable the Claude model for the project (terms + quota).
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
open "$(terraform output -raw console_url)"          # sign in with a token from the riverbend-staff-tokens secret
```

After that, every merge to `main` runs `deploy/cloudbuild.yaml`: tests (against a throwaway Postgres) → build both images → push → **migrate** → roll the worker, then the API, then the console. Infrastructure changes go through `terraform plan` in review. The pipeline only ships images.

## Configuration flow

`src/riverbend/config.py` is the only reader of configuration. It is a typed `Settings` class, and secrets are `SecretStr` so they never appear in logs or tracebacks.
- **Locally:** `.env`, which is git-ignored. `.env.example` documents every variable.
- **On Cloud Run:** plain env vars from Terraform, plus secrets mounted from Secret Manager.

The code cannot tell the two apart. No key is committed anywhere. On GCP the model credential is the service account itself.

## Rough monthly cost (list prices, us-central1; check with the GCP calculator)

| Item | Estimate | Note |
|---|---|---|
| Cloud SQL, 1 vCPU / 3.75 GB, zonal, 10 GB SSD + backups | ~$55 | HA (`REGIONAL`) roughly doubles it |
| 2 always-on workers, 1 vCPU / 512 MiB | ~$90–100 | The largest line. One worker at 0.5 vCPU is ~$25 and is enough for three shops. |
| API, console, Secret Manager, Artifact Registry, logging | < $10 | Both services scale to zero |
| Claude on Vertex | ~$0.02 per message on Opus 5.5 (simulated estimate; confirm with `make eval-live`) | ~1,000 messages a month ≈ $20. Haiku 5.5 would be ~40× cheaper if the eval holds up. |

For three shops the fixed cost (always-on workers + Cloud SQL) dominates, not the model. That is the main argument *against* decision 1 in the architecture note. A push model (Cloud Tasks → Cloud Run, scale to zero) would cut ~$80/month at the price of a second moving part.

## Production gaps to close before real customers

Staff routes and the console currently rely on per-shop bearer tokens. Put **Identity-Aware Proxy / Identity Platform** in front of the console and the API's staff routes, with the shop as a claim. Add **Cloud Armor** rate limits on intake. Move the database to a `REGIONAL` instance. Add an outbox **sender**: the outbox is written, but delivery is not built. All of these are in [NOT_BUILT.md](NOT_BUILT.md).
