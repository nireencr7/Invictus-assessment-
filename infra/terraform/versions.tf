terraform {
  required_version = ">= 1.6"

  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 6.10"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.6"
    }
  }

  # State contains generated database passwords: keep it in a private, versioned bucket.
  # terraform init -backend-config="bucket=<your-state-bucket>"
  backend "gcs" {
    prefix = "riverbend/state"
  }
}

provider "google" {
  project = var.project_id
  region  = var.region
}
