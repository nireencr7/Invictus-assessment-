# Riverbend work orders on GCP.
#
#   Cloud Run  api      — public HTTPS intake + staff API (scales to zero is fine; it never calls the model)
#   Cloud Run  worker   — always-on queue consumers (CPU always allocated); the only thing that calls Claude
#   Cloud Run  job      — migrations, executed by CI before each rollout
#   Cloud SQL  Postgres — data, queue and spend ledger; RLS enforces tenancy
#   Vertex AI  Claude   — called with the worker's service account; no API key exists anywhere
#   Secret Manager      — DB DSNs, staff tokens, ingest keys, admin token

locals {
  apis = [
    "run.googleapis.com",
    "sqladmin.googleapis.com",
    "secretmanager.googleapis.com",
    "artifactregistry.googleapis.com",
    "aiplatform.googleapis.com",
    "monitoring.googleapis.com",
    "logging.googleapis.com",
    "iam.googleapis.com",
    "cloudbuild.googleapis.com",
    "cloudresourcemanager.googleapis.com",
    "iamcredentials.googleapis.com", # GitHub Actions: keyless deploys (cicd.tf)
    "sts.googleapis.com",
  ]
}

resource "google_project_service" "apis" {
  for_each           = toset(local.apis)
  service            = each.value
  disable_on_destroy = false
}

resource "google_artifact_registry_repository" "app" {
  repository_id = "riverbend"
  location      = var.region
  format        = "DOCKER"
  depends_on    = [google_project_service.apis]
}

# --- identities: one service account per role, least privilege -----------------------

resource "google_service_account" "api" {
  account_id   = "riverbend-api"
  display_name = "Riverbend API (intake + staff reads)"
  depends_on   = [google_project_service.apis]
}

resource "google_service_account" "worker" {
  account_id   = "riverbend-worker"
  display_name = "Riverbend worker (calls Claude on Vertex AI)"
  depends_on   = [google_project_service.apis]
}

resource "google_service_account" "migrate" {
  account_id   = "riverbend-migrate"
  display_name = "Riverbend migrations job"
  depends_on   = [google_project_service.apis]
}

# Builds run as their own identity with exactly what they need. (Organisations that disable automatic
# grants to default service accounts leave the default Cloud Build identity with no permissions.)
resource "google_service_account" "build" {
  account_id   = "riverbend-build"
  display_name = "Riverbend image builds"
  depends_on   = [google_project_service.apis]
}

resource "google_project_iam_member" "build" {
  for_each = toset(["roles/artifactregistry.writer", "roles/logging.logWriter", "roles/storage.objectViewer"])
  project  = var.project_id
  role     = each.value
  member   = "serviceAccount:${google_service_account.build.email}"
}

resource "google_project_iam_member" "sql_client" {
  for_each = {
    api     = google_service_account.api.email
    worker  = google_service_account.worker.email
    migrate = google_service_account.migrate.email
  }
  project = var.project_id
  role    = "roles/cloudsql.client"
  member  = "serviceAccount:${each.value}"
}

# Only the worker may call the model. The API process cannot spend LLM budget even if compromised.
resource "google_project_iam_member" "worker_vertex" {
  project = var.project_id
  role    = "roles/aiplatform.user"
  member  = "serviceAccount:${google_service_account.worker.email}"
}
