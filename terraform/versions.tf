terraform {
  required_version = ">= 1.5"
  required_providers {
    google = {
      source  = "hashicorp/google"
      version = ">= 6.11" # control_plane_endpoints_config (DNS endpoint) landed in 6.11.0
    }
  }
}

provider "google" {
  project = var.project_id
  region  = var.region
}
