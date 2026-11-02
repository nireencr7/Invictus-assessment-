# Continuous deployment from GitHub Actions, with no long-lived key anywhere.
#
# A workflow run on this repository's main branch gets an OIDC token from GitHub. Workload Identity
# Federation exchanges it for a short-lived token of riverbend-deploy, which may only push images,
# update the Cloud Run services and the migration job, run that job, and act as the services' own
# accounts. Other repositories, other branches, forks and pull requests get nothing.
#
# Terraform owns the infrastructure; the pipeline owns which image is running (see the lifecycle
# blocks in run.tf), so a later `terraform apply` never rolls back a deploy.

resource "google_iam_workload_identity_pool" "github" {
  workload_identity_pool_id = "github"
  display_name              = "GitHub Actions"
  depends_on                = [google_project_service.apis]
}

resource "google_iam_workload_identity_pool_provider" "github" {
  workload_identity_pool_id          = google_iam_workload_identity_pool.github.workload_identity_pool_id
  workload_identity_pool_provider_id = "github-actions"
  display_name                       = "GitHub Actions OIDC"
  attribute_mapping = {
    "google.subject"       = "assertion.sub"
    "attribute.repository" = "assertion.repository"
    "attribute.ref"        = "assertion.ref"
  }
  attribute_condition = "assertion.repository == \"${var.github_repository}\" && assertion.ref == \"refs/heads/main\""
  oidc {
    issuer_uri = "https://token.actions.githubusercontent.com"
  }
}

resource "google_service_account" "deploy" {
  account_id   = "riverbend-deploy"
  display_name = "Riverbend deployments from GitHub Actions"
  depends_on   = [google_project_service.apis]
}

resource "google_service_account_iam_member" "deploy_from_github" {
  service_account_id = google_service_account.deploy.name
  role               = "roles/iam.workloadIdentityUser"
  member             = "principalSet://iam.googleapis.com/${google_iam_workload_identity_pool.github.name}/attribute.repository/${var.github_repository}"
}

resource "google_artifact_registry_repository_iam_member" "deploy_push" {
  repository = google_artifact_registry_repository.app.name
  location   = google_artifact_registry_repository.app.location
  role       = "roles/artifactregistry.writer"
  member     = "serviceAccount:${google_service_account.deploy.email}"
}

# Update services and jobs, and run the migration job. Project-wide, but this project runs only Riverbend.
resource "google_project_iam_member" "deploy_run" {
  project = var.project_id
  role    = "roles/run.developer"
  member  = "serviceAccount:${google_service_account.deploy.email}"
}

# A new revision runs as its service's own account; deploying it requires permission to act as that account.
resource "google_service_account_iam_member" "deploy_acts_as" {
  for_each = {
    api     = google_service_account.api.name
    worker  = google_service_account.worker.name
    migrate = google_service_account.migrate.name
    console = google_service_account.console.name
  }
  service_account_id = each.value
  role               = "roles/iam.serviceAccountUser"
  member             = "serviceAccount:${google_service_account.deploy.email}"
}

output "github_workload_identity_provider" {
  value       = google_iam_workload_identity_pool_provider.github.name
  description = "GitHub repository variable GCP_WORKLOAD_IDENTITY_PROVIDER"
}

output "deploy_service_account" {
  value       = google_service_account.deploy.email
  description = "GitHub repository variable GCP_DEPLOY_SERVICE_ACCOUNT"
}
