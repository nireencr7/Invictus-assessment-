# Cloud SQL for PostgreSQL 16. Reached through the Cloud SQL connector (unix socket mounted
# into Cloud Run), which authenticates with IAM and encrypts in transit; no authorized networks.

resource "google_sql_database_instance" "main" {
  name                = "riverbend-pg"
  database_version    = "POSTGRES_16"
  region              = var.region
  deletion_protection = true

  settings {
    tier              = var.db_tier
    availability_type = "ZONAL" # REGIONAL (HA) is the first thing to buy when this matters more
    disk_autoresize   = true

    backup_configuration {
      enabled                        = true
      point_in_time_recovery_enabled = true
      start_time                     = "03:00"
    }

    ip_configuration {
      ipv4_enabled = true # public IP but no authorized networks: only the connector can reach it
      ssl_mode     = "ENCRYPTED_ONLY"
    }

    database_flags {
      name  = "max_connections"
      value = "100"
    }

    insights_config {
      query_insights_enabled = true
    }
  }

  depends_on = [google_project_service.apis]
}

resource "google_sql_database" "app" {
  name     = "riverbend"
  instance = google_sql_database_instance.main.name
}

resource "random_password" "owner" {
  length  = 32
  special = false
}

resource "random_password" "app" {
  length  = 32
  special = false
}

# Owner: runs migrations. `riverbend_app` (the runtime role, no BYPASSRLS) is created by the
# migrations themselves with APP_DB_PASSWORD, so its privileges live in code review, not console clicks.
resource "google_sql_user" "owner" {
  name     = "riverbend_owner"
  instance = google_sql_database_instance.main.name
  password = random_password.owner.result
}

locals {
  socket    = "/cloudsql/${google_sql_database_instance.main.connection_name}"
  app_dsn   = "postgresql://riverbend_app:${random_password.app.result}@/riverbend?host=${local.socket}"
  owner_dsn = "postgresql://riverbend_owner:${random_password.owner.result}@/riverbend?host=${local.socket}"
}

# --- secrets ------------------------------------------------------------------------------

resource "random_password" "admin_token" {
  length  = 40
  special = false
}

resource "random_password" "ingest_key" {
  length  = 40
  special = false
}

resource "random_password" "staff_token" {
  for_each = toset(["rb-downtown", "rb-northside", "rb-lakeshore"])
  length   = 40
  special  = false
}

locals {
  secrets = {
    "riverbend-database-url"       = local.app_dsn
    "riverbend-database-admin-url" = local.owner_dsn
    "riverbend-app-db-password"    = random_password.app.result
    "riverbend-admin-token"        = random_password.admin_token.result
    "riverbend-ingest-keys"        = jsonencode({ (random_password.ingest_key.result) = ["rb-downtown", "rb-northside", "rb-lakeshore"] })
    # Interim staff auth: one bearer token per shop. Replace with Identity-Aware Proxy / Identity Platform
    # (see docs/NOT_BUILT.md) — the shop binding then comes from the user's identity claims.
    "riverbend-staff-tokens" = jsonencode({ for shop, pw in random_password.staff_token : pw.result => shop })
  }
}

resource "google_secret_manager_secret" "s" {
  for_each  = local.secrets
  secret_id = each.key
  replication {
    auto {}
  }
  depends_on = [google_project_service.apis]
}

resource "google_secret_manager_secret_version" "s" {
  for_each    = local.secrets
  secret      = google_secret_manager_secret.s[each.key].id
  secret_data = each.value
}

# Who may read which secret. The API never sees the owner DSN; only the migrate job does.
locals {
  secret_readers = {
    "riverbend-database-url"       = [google_service_account.api.email, google_service_account.worker.email]
    "riverbend-database-admin-url" = [google_service_account.migrate.email]
    "riverbend-app-db-password"    = [google_service_account.migrate.email]
    "riverbend-admin-token"        = [google_service_account.api.email]
    "riverbend-ingest-keys"        = [google_service_account.api.email]
    "riverbend-staff-tokens"       = [google_service_account.api.email]
  }
  secret_bindings = merge([
    for secret, readers in local.secret_readers : {
      for r in readers : "${secret}/${r}" => { secret = secret, member = r }
    }
  ]...)
}

resource "google_secret_manager_secret_iam_member" "readers" {
  for_each  = local.secret_bindings
  secret_id = google_secret_manager_secret.s[each.value.secret].id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${each.value.member}"
}
