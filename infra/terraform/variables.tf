variable "project_id" {
  description = "GCP project to deploy into."
  type        = string
}

variable "region" {
  description = "Region for Cloud Run, Cloud SQL and Artifact Registry."
  type        = string
  default     = "us-central1"
}

variable "image" {
  description = "Container image, e.g. us-central1-docker.pkg.dev/<project>/riverbend/app:<git-sha>. Set by CI."
  type        = string
}

variable "llm_model" {
  description = "Claude model id on Vertex AI."
  type        = string
  default     = "claude-opus-5-5"
}

variable "vertex_region" {
  description = "Vertex AI location for Claude ('global' recommended)."
  type        = string
  default     = "global"
}

variable "db_tier" {
  description = "Cloud SQL machine tier. db-custom-1-3840 is ample for three shops."
  type        = string
  default     = "db-custom-1-3840"
}

variable "worker_count" {
  description = "Always-on worker instances. Two gives overlap during deploys; SKIP LOCKED makes it safe."
  type        = number
  default     = 2
}

variable "default_monthly_cap_usd" {
  description = "Initial per-shop monthly LLM spend cap."
  type        = string
  default     = "5.00"
}

variable "alert_email" {
  description = "Where budget / escalation / outage alerts go."
  type        = string
}
