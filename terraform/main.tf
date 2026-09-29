# Sandbox infrastructure for dogfooding Gojira in-cluster.
#
# Stands up: required APIs, an Autopilot GKE cluster, an Artifact Registry repo
# for Gojira's own image, and Gojira's Google Service Account wired to its
# in-cluster KSA via Workload Identity.
#
# NOTE — sandbox only. Gojira *provisions IAM* (it creates per-app service
# accounts, WIF pools, project IAM bindings, Artifact Registry repos), so its
# own GSA is granted broad project-admin roles below. That concentration of
# privilege is acceptable for a throwaway sandbox and nowhere else. A hosted /
# production Gojira would scope these down per operation.

locals {
  required_apis = [
    "container.googleapis.com",
    "artifactregistry.googleapis.com",
    "iam.googleapis.com",
    "iamcredentials.googleapis.com",
    "sts.googleapis.com",
    "cloudresourcemanager.googleapis.com",
    "serviceusage.googleapis.com",
  ]

  # Broad because Gojira creates GSAs/WIF/AR and sets project IAM. Sandbox only.
  # KEEP IN SYNC with scripts/prepare-project.sh (the brownfield path).
  gojira_roles = [
    "roles/container.admin",                # kubectl apply, incl. the cluster-scoped
                                            # RBAC the agent deploy writes (finding #11)
    "roles/artifactregistry.admin",         # create per-app AR repos, push images
    "roles/iam.serviceAccountAdmin",        # create per-app deploy GSAs
    "roles/iam.workloadIdentityPoolAdmin",  # create the GitHub OIDC WIF pool/provider
    "roles/resourcemanager.projectIamAdmin", # bind roles to the per-app GSAs
    "roles/serviceusage.serviceUsageAdmin", # enable APIs on first provision
  ]
}

resource "google_project_service" "enabled" {
  for_each           = toset(local.required_apis)
  service            = each.value
  disable_on_destroy = false
}

resource "google_container_cluster" "sandbox" {
  name             = var.cluster_name
  location         = var.region
  enable_autopilot = true

  # Autopilot enables Workload Identity automatically (project pool). Deletion
  # protection off — this is a throwaway.
  deletion_protection = false

  # Gojira reaches every control plane over GKE's IAM-authenticated DNS
  # endpoint (registration probe, generated CI from GitHub runners — finding
  # #14). On at birth here; registration enables it on clusters made elsewhere.
  control_plane_endpoints_config {
    dns_endpoint_config {
      allow_external_traffic = true
    }
  }

  depends_on = [google_project_service.enabled]
}

resource "google_artifact_registry_repository" "gojira" {
  repository_id = var.ar_repo
  location      = var.region
  format        = "DOCKER"
  description   = "Gojira's own image (dogfood)."
  depends_on    = [google_project_service.enabled]
}

resource "google_service_account" "gojira" {
  account_id   = "gojira"
  display_name = "Gojira deploy platform (sandbox)"
  depends_on   = [google_project_service.enabled]
}

resource "google_project_iam_member" "gojira" {
  for_each = toset(local.gojira_roles)
  project  = var.project_id
  role     = each.value
  member   = "serviceAccount:${google_service_account.gojira.email}"
}

# Workload Identity: let the in-cluster KSA impersonate the GSA.
# The <project>.svc.id.goog identity pool comes into being with the first
# Workload-Identity cluster; nothing in this binding's own arguments names the
# cluster, so without depends_on terraform applies it first and IAM answers
# "Identity Pool does not exist". Hit on the first apply into a fresh
# project (dogfood, 2026-09-29); a second apply then succeeded.
resource "google_service_account_iam_member" "wi" {
  service_account_id = google_service_account.gojira.name
  role               = "roles/iam.workloadIdentityUser"
  member             = "serviceAccount:${var.project_id}.svc.id.goog[${var.gojira_namespace}/${var.gojira_ksa}]"

  depends_on = [google_container_cluster.sandbox]
}
