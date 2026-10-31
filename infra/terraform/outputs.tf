output "api_url" {
  value = google_cloud_run_v2_service.api.uri
}

output "artifact_registry" {
  value = "${var.region}-docker.pkg.dev/${var.project_id}/${google_artifact_registry_repository.app.repository_id}"
}

output "cloudsql_connection_name" {
  value = google_sql_database_instance.main.connection_name
}

output "staff_token_secret" {
  value       = google_secret_manager_secret.s["riverbend-staff-tokens"].secret_id
  description = "Read with: gcloud secrets versions access latest --secret=riverbend-staff-tokens"
}
