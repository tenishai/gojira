variable "project_id" {
  type        = string
  description = "Existing sandbox GCP project to stand the dogfood up in (must have billing)."
}

variable "region" {
  type        = string
  description = "Region for the Autopilot cluster and Artifact Registry."
  default     = "us-central1"
}

variable "cluster_name" {
  type        = string
  description = "Name of the throwaway sandbox GKE cluster."
  default     = "gojira-sandbox"
}

variable "ar_repo" {
  type        = string
  description = "Artifact Registry repo that holds Gojira's own image (apps get their own, created by Gojira)."
  default     = "gojira"
}

variable "gojira_namespace" {
  type        = string
  description = "Namespace Gojira runs in, used for the Workload Identity binding."
  default     = "gojira"
}

variable "gojira_ksa" {
  type        = string
  description = "Kubernetes ServiceAccount Gojira's pod uses, bound to the GSA via Workload Identity."
  default     = "gojira"
}
