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
  description = "Model id on Vertex AI: a Claude id for llm_provider=vertex, a Gemini id (e.g. gemini-3.8-flash) for gemini."
  type        = string
  default     = "claude-opus-5-5"
}

variable "vertex_region" {
  description = "Vertex AI location for the model ('global' recommended)."
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
  description = "Where budget / escalation / outage alerts go. Empty: alert policies exist but notify no one."
  type        = string
  default     = ""
}

variable "llm_provider" {
  description = "vertex (Claude on Vertex AI), gemini (Gemini on Vertex AI) or offline (the labelled stand-in, $0)."
  type        = string
  default     = "vertex"
  validation {
    condition     = contains(["vertex", "gemini", "offline"], var.llm_provider)
    error_message = "llm_provider must be vertex, gemini or offline."
  }
}

variable "api_max_instances" {
  description = "Upper bound on API instances. Each holds up to 10 DB connections, so keep it under db_max_connections / 10."
  type        = number
  default     = 10
}

variable "db_max_connections" {
  description = "Postgres max_connections."
  type        = number
  default     = 100
}

variable "test_messages" {
  description = "Let signed-in staff send messages as a customer of their own shop from the console, to try the workflow. On for a demo, off for real shops."
  type        = bool
  default     = false
}

variable "deletion_protection" {
  description = "Protect Cloud SQL and Cloud Run from deletion. false for a demo, so `terraform destroy` tears everything down."
  type        = bool
  default     = true
}

variable "console_image" {
  description = "Staff console image, e.g. us-central1-docker.pkg.dev/<project>/riverbend/console:<git-sha>. Set by CI."
  type        = string
}

variable "github_repository" {
  description = "The GitHub repository allowed to deploy, as owner/name. Only its main branch can (cicd.tf)."
  type        = string
}
