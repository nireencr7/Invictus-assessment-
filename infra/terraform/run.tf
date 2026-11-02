locals {
  common_env = {
    LLM_PROVIDER            = var.llm_provider
    LLM_MODEL               = var.llm_model
    VERTEX_PROJECT          = var.project_id
    VERTEX_REGION           = var.vertex_region
    DEFAULT_MONTHLY_CAP_USD = var.default_monthly_cap_usd
    DEV_MODE                = "false"
    TEST_MESSAGES           = tostring(var.test_messages)
    LOG_LEVEL               = "INFO"
  }
}

# --- API: intake + staff reads. Never calls the model. --------------------------------------

resource "google_cloud_run_v2_service" "api" {
  name                = "riverbend-api"
  location            = var.region
  ingress             = "INGRESS_TRAFFIC_ALL"
  deletion_protection = var.deletion_protection

  template {
    service_account = google_service_account.api.email
    scaling {
      min_instance_count = 0
      max_instance_count = var.api_max_instances
    }
    volumes {
      name = "cloudsql"
      cloud_sql_instance {
        instances = [google_sql_database_instance.main.connection_name]
      }
    }
    containers {
      image = var.image
      args  = ["sh", "-c", "exec python -m riverbend api --port $PORT"]
      resources {
        limits   = { cpu = "1", memory = "512Mi" }
        cpu_idle = true # request-based CPU: billed only while serving
      }
      dynamic "env" {
        for_each = local.common_env
        content {
          name  = env.key
          value = env.value
        }
      }
      dynamic "env" {
        for_each = {
          DATABASE_URL = "riverbend-database-url"
          ADMIN_TOKEN  = "riverbend-admin-token"
          INGEST_KEYS  = "riverbend-ingest-keys"
          STAFF_TOKENS = "riverbend-staff-tokens"
        }
        content {
          name = env.key
          value_source {
            secret_key_ref {
              secret  = google_secret_manager_secret.s[env.value].secret_id
              version = "latest"
            }
          }
        }
      }
      volume_mounts {
        name       = "cloudsql"
        mount_path = "/cloudsql"
      }
      startup_probe {
        http_get { path = "/healthz" }
      }
      liveness_probe {
        http_get { path = "/healthz" }
      }
    }
  }

  depends_on = [google_secret_manager_secret_iam_member.readers]

  lifecycle {
    # The deploy pipeline (.github/workflows/ci.yml) sets the running image; Terraform owns the rest.
    # Service-level `scaling` is unused; the provider reads it back as zeros and would "remove" it.
    ignore_changes = [template[0].containers[0].image, client, client_version, scaling]
  }
}

# Intake must be reachable by the email gateway / web form. Auth is the ingest key (and staff tokens)
# checked by the app; put Cloud Armor / IAP in front for staff routes in production.
resource "google_cloud_run_v2_service_iam_member" "api_public" {
  name     = google_cloud_run_v2_service.api.name
  location = var.region
  role     = "roles/run.invoker"
  member   = "allUsers"
}

# --- Worker: always-on consumers; the only component allowed to call Claude. -----------------

resource "google_cloud_run_v2_service" "worker" {
  name                = "riverbend-worker"
  location            = var.region
  ingress             = "INGRESS_TRAFFIC_INTERNAL_ONLY"
  deletion_protection = var.deletion_protection

  template {
    service_account = google_service_account.worker.email
    scaling {
      min_instance_count = var.worker_count
      max_instance_count = var.worker_count
    }
    volumes {
      name = "cloudsql"
      cloud_sql_instance {
        instances = [google_sql_database_instance.main.connection_name]
      }
    }
    containers {
      image = var.image
      args  = ["sh", "-c", "exec python -m riverbend worker --health-port $PORT"]
      resources {
        limits   = { cpu = "1", memory = "512Mi" }
        cpu_idle = false # pull-based: CPU must stay allocated between requests
      }
      dynamic "env" {
        for_each = local.common_env
        content {
          name  = env.key
          value = env.value
        }
      }
      env {
        name = "DATABASE_URL"
        value_source {
          secret_key_ref {
            secret  = google_secret_manager_secret.s["riverbend-database-url"].secret_id
            version = "latest"
          }
        }
      }
      volume_mounts {
        name       = "cloudsql"
        mount_path = "/cloudsql"
      }
      startup_probe {
        http_get { path = "/healthz" }
      }
    }
  }

  depends_on = [google_secret_manager_secret_iam_member.readers]

  lifecycle {
    # The deploy pipeline (.github/workflows/ci.yml) sets the running image; Terraform owns the rest.
    # Service-level `scaling` is unused; the provider reads it back as zeros and would "remove" it.
    ignore_changes = [template[0].containers[0].image, client, client_version, scaling]
  }
}

# --- Migrations: run by CI before each rollout (gcloud run jobs execute riverbend-migrate --wait) -

resource "google_cloud_run_v2_job" "migrate" {
  name                = "riverbend-migrate"
  location            = var.region
  deletion_protection = var.deletion_protection

  template {
    template {
      service_account = google_service_account.migrate.email
      max_retries     = 1
      volumes {
        name = "cloudsql"
        cloud_sql_instance {
          instances = [google_sql_database_instance.main.connection_name]
        }
      }
      containers {
        image = var.image
        args  = ["python", "-m", "riverbend", "migrate"]
        env {
          name  = "DEFAULT_MONTHLY_CAP_USD"
          value = var.default_monthly_cap_usd
        }
        dynamic "env" {
          for_each = {
            DATABASE_ADMIN_URL = "riverbend-database-admin-url"
            APP_DB_PASSWORD    = "riverbend-app-db-password"
          }
          content {
            name = env.key
            value_source {
              secret_key_ref {
                secret  = google_secret_manager_secret.s[env.value].secret_id
                version = "latest"
              }
            }
          }
        }
        volume_mounts {
          name       = "cloudsql"
          mount_path = "/cloudsql"
        }
      }
    }
  }

  depends_on = [google_secret_manager_secret_iam_member.readers]

  lifecycle {
    # The deploy pipeline (.github/workflows/ci.yml) sets the image; Terraform owns the rest.
    ignore_changes = [template[0].template[0].containers[0].image, client, client_version]
  }
}

# --- Staff console: a separate static app (nginx + React build), deployed and rolled back on its own.
# It reaches the API only through its own /v1 reverse proxy, so the browser sees one origin and the
# API needs no CORS. It holds no secrets and needs no IAM roles.

resource "google_service_account" "console" {
  account_id   = "riverbend-console"
  display_name = "Riverbend staff console (static files + proxy; no permissions)"
  depends_on   = [google_project_service.apis]
}

resource "google_cloud_run_v2_service" "console" {
  name                = "riverbend-console"
  location            = var.region
  ingress             = "INGRESS_TRAFFIC_ALL"
  deletion_protection = var.deletion_protection

  template {
    service_account = google_service_account.console.email
    scaling {
      min_instance_count = 0
      max_instance_count = 3
    }
    containers {
      image = var.console_image
      resources {
        limits   = { cpu = "1", memory = "256Mi" }
        cpu_idle = true # request-based CPU (always-on CPU would need >= 512Mi)
      }
      env {
        name  = "API_UPSTREAM"
        value = google_cloud_run_v2_service.api.uri
      }
      env {
        name  = "CONSOLE_MODE"
        value = "live"
      }
      startup_probe {
        http_get { path = "/console-healthz" }
      }
    }
  }

  lifecycle {
    # The deploy pipeline (.github/workflows/ci.yml) sets the running image; Terraform owns the rest.
    # Service-level `scaling` is unused; the provider reads it back as zeros and would "remove" it.
    ignore_changes = [template[0].containers[0].image, client, client_version, scaling]
  }
}

# Public like the API: sign-in is the staff token. Put Identity-Aware Proxy in front of this service
# (and of the API's staff routes) before real staff use it — see docs/NOT_BUILT.md.
resource "google_cloud_run_v2_service_iam_member" "console_public" {
  name     = google_cloud_run_v2_service.console.name
  location = var.region
  role     = "roles/run.invoker"
  member   = "allUsers"
}
