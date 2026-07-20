#!/usr/bin/env bash
# Prepare an EXISTING GCP project to host Gojira — the brownfield mirror of
# terraform/. Where terraform/ builds a sandbox from nothing (project APIs,
# GSA, roles, AR, plus the cluster itself), this script grants the same
# platform identity into a project whose cluster already exists and is
# managed elsewhere. Born 2026-07-18, the day Gojira met a real fleet
# cluster that terraform did not create.
#
#   PROJECT=my-project ./scripts/prepare-project.sh
#
# What it does, in order (each step idempotent — rerun freely):
#   1. enable required APIs
#   2. create the gojira GSA
#   3. grant the platform roles     (KEEP IN SYNC with terraform/main.tf
#                                    gojira_roles — the one source of truth
#                                    for what Gojira is allowed to do)
#   4. create the AR repo for the Gojira image
#   5. bind Workload Identity       (KSA gojira/gojira -> the GSA)
#
# Run it as a human with owner-level rights on the project: granting the
# platform its identity is exactly the moment an operator should be present.
# Next step after this: create the GitHub App (docs/github-app-setup.md),
# then ./scripts/bootstrap.sh with CLUSTER pointed at your existing cluster.
#
# NB: same-project clusters only. Registering a cluster from a DIFFERENT
# project will need cross-project grants this script does not attempt.

set -euo pipefail

PROJECT="${PROJECT:?set PROJECT to the GCP project that will host Gojira}"
REGION="${REGION:-us-central1}"

GSA="gojira@${PROJECT}.iam.gserviceaccount.com"
AR_PATH="${REGION}-docker.pkg.dev/${PROJECT}/gojira"

step() { printf '\n\033[1m== %s\033[0m\n' "$*"; }

command -v gcloud >/dev/null || { echo "error: gcloud not installed" >&2; exit 1; }

APIS=(
  container.googleapis.com
  artifactregistry.googleapis.com
  cloudbuild.googleapis.com
  iam.googleapis.com
  iamcredentials.googleapis.com
  sts.googleapis.com
  cloudresourcemanager.googleapis.com
  serviceusage.googleapis.com
)

# Keep in sync with terraform/main.tf (locals.gojira_roles).
ROLES=(
  roles/container.admin                 # kubectl apply incl. cluster-scoped RBAC (finding #11)
  roles/artifactregistry.admin          # create per-app AR repos, push images
  roles/iam.serviceAccountAdmin         # create per-app deploy GSAs
  roles/iam.workloadIdentityPoolAdmin   # create the GitHub OIDC WIF pool/provider
  roles/resourcemanager.projectIamAdmin # bind roles to the per-app GSAs
  roles/serviceusage.serviceUsageAdmin  # enable APIs on first provision
)

step "1/5 enable APIs on $PROJECT"
gcloud services enable "${APIS[@]}" --project "$PROJECT"

step "2/5 service account $GSA"
if gcloud iam service-accounts describe "$GSA" --project "$PROJECT" >/dev/null 2>&1; then
  echo "already exists"
else
  gcloud iam service-accounts create gojira \
    --display-name "Gojira deploy platform" --project "$PROJECT"
fi

step "3/5 platform roles"
for role in "${ROLES[@]}"; do
  gcloud projects add-iam-policy-binding "$PROJECT" \
    --member "serviceAccount:$GSA" --role "$role" \
    --condition=None --quiet >/dev/null
  echo "granted $role"
done

step "4/5 Artifact Registry repo $AR_PATH"
if gcloud artifacts repositories describe gojira \
     --location "$REGION" --project "$PROJECT" >/dev/null 2>&1; then
  echo "already exists"
else
  gcloud artifacts repositories create gojira --repository-format docker \
    --location "$REGION" --project "$PROJECT" \
    --description "Gojira deploy platform images"
fi

step "5/5 Workload Identity: gojira/gojira -> $GSA"
gcloud iam service-accounts add-iam-policy-binding "$GSA" --project "$PROJECT" \
  --role roles/iam.workloadIdentityUser \
  --member "serviceAccount:${PROJECT}.svc.id.goog[gojira/gojira]" --quiet >/dev/null
echo "bound"

printf '\nproject ready. next: the GitHub App (docs/github-app-setup.md), then:\n'
printf '  PROJECT=%s REGION=%s CLUSTER=<your-cluster> GOJIRA_GITHUB_APP_ID=... GOJIRA_GITHUB_APP_PEM=... ./scripts/bootstrap.sh\n' "$PROJECT" "$REGION"
