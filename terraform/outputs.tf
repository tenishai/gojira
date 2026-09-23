output "cluster_name" {
  value       = google_container_cluster.sandbox.name
  description = "Register this as a Gojira cluster (location = region)."
}

output "cluster_location" {
  value = google_container_cluster.sandbox.location
}

output "gojira_gsa_email" {
  value       = google_service_account.gojira.email
  description = "Annotate the gojira KSA with this for Workload Identity."
}

output "ar_repo_path" {
  value       = "${var.region}-docker.pkg.dev/${var.project_id}/${var.ar_repo}"
  description = "Push Gojira's image here."
}

output "get_credentials_cmd" {
  value = "gcloud container clusters get-credentials ${google_container_cluster.sandbox.name} --region ${var.region} --project ${var.project_id} --dns-endpoint"
}
