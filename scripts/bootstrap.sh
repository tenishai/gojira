#!/usr/bin/env bash
# Bootstrap Gojira into a sandbox cluster — everything after `terraform apply`.
#
#   PROJECT=my-sandbox \
#   GOJIRA_GITHUB_APP_ID=1234567 \
#   GOJIRA_GITHUB_APP_PEM=./github-app.pem \
#   ./scripts/bootstrap.sh
#
# Optional front door (IAP + HTTPS LB; see deploy/ingress/):
#   GOJIRA_HOSTNAME=gojira.example.com     enables the ingress step
#   GOJIRA_IAP_MEMBERS=domain:example.com  comma-separated IAP accessor grants
# After it provisions, point DNS (A record) at the printed static IP and set
# the auth: block in your config (enabled + allowed_domain/allowed_emails).
#
# What it does, in order (each step idempotent — rerun freely):
#   1. cluster credentials        (gcloud get-credentials)
#   2. build + push the image     (Cloud Build — works from ARM machines too)
#   3. namespace + GitHub App secret
#   4. render + apply deploy/     (placeholders filled in a temp copy; your
#                                  working tree stays clean)
#   5. wait for the pod, print how to reach the UI
#   6. (only with GOJIRA_HOSTNAME) static IP, IAP OAuth client, ingress
#
# It deliberately does NOT read terraform state: every value is derived from
# PROJECT/REGION and the conventions in terraform/ (GSA `gojira@<project>`,
# AR repo `gojira`, cluster `gojira-sandbox`), so it runs from any machine —
# terraform can live on a different one.

set -euo pipefail

# --- inputs -------------------------------------------------------------------
PROJECT="${PROJECT:?set PROJECT to your sandbox GCP project id}"
REGION="${REGION:-us-central1}"
CLUSTER="${CLUSTER:-gojira-sandbox}"
# NB: no apostrophes in :? messages — inside ${VAR:?...} a quote character
# starts a quote context even within double quotes and eats the script.
APP_ID="${GOJIRA_GITHUB_APP_ID:?set GOJIRA_GITHUB_APP_ID (the GitHub App numeric id)}"
PEM="${GOJIRA_GITHUB_APP_PEM:?set GOJIRA_GITHUB_APP_PEM (path to the App private key .pem)}"
[ -f "$PEM" ] || { echo "error: pem not found at $PEM" >&2; exit 1; }

AR_PATH="${REGION}-docker.pkg.dev/${PROJECT}/gojira"
GSA="gojira@${PROJECT}.iam.gserviceaccount.com"
REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"

step() { printf '\n\033[1m== %s\033[0m\n' "$*"; }

for tool in gcloud kubectl; do
  command -v "$tool" >/dev/null || { echo "error: $tool not installed" >&2; exit 1; }
done

step "1/5 cluster credentials ($CLUSTER @ $PROJECT/$REGION)"
# GOJIRA_DNS_ENDPOINT=1: reach a private-endpoint cluster over GKE's
# IAM-authenticated DNS endpoint (brownfield clusters often have no public
# endpoint and a VPN-only private one; the operator machine may be on
# neither). Requires allowExternalTraffic on the cluster's dnsEndpointConfig.
gcloud container clusters get-credentials "$CLUSTER" --region "$REGION" --project "$PROJECT" \
  ${GOJIRA_DNS_ENDPOINT:+--dns-endpoint}

step "2/5 build + push image via Cloud Build (avoids the ARM/amd64 trap)"
gcloud services enable cloudbuild.googleapis.com --project "$PROJECT" --quiet
gcloud builds submit --tag "${AR_PATH}/gojira:dev" --project "$PROJECT" "$REPO_ROOT"

step "3/5 namespace + GitHub App secret"
kubectl create namespace gojira --dry-run=client -o yaml | kubectl apply -f -
# Preserve an existing webhook secret across reruns; mint one on first run.
WEBHOOK_SECRET="$(kubectl -n gojira get secret gojira-github-app \
  -o jsonpath='{.data.webhook_secret}' 2>/dev/null | base64 -d || true)"
WEBHOOK_SECRET="${WEBHOOK_SECRET:-$(openssl rand -hex 32)}"
kubectl -n gojira create secret generic gojira-github-app \
  --from-literal=app_id="$APP_ID" \
  --from-literal=webhook_secret="$WEBHOOK_SECRET" \
  --from-file=private_key="$PEM" \
  --dry-run=client -o yaml | kubectl apply -f -

step "4/5 render + apply manifests (temp copy — your tree stays clean)"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
cp -r "$REPO_ROOT/deploy/." "$TMP/"
sed -i.bak "s#REPLACE_GSA_EMAIL#${GSA}#" "$TMP/serviceaccount.yaml"
sed -i.bak "s#REPLACE_AR_PATH#${AR_PATH}#" "$TMP/kustomization.yaml"
rm -f "$TMP"/*.bak
kubectl apply -k "$TMP"

step "5/5 wait for Gojira to come up"
kubectl -n gojira rollout status deploy/gojira --timeout=300s

if [ -n "${GOJIRA_HOSTNAME:-}" ]; then
  step "6 (optional) front door: IAP + HTTPS LB for $GOJIRA_HOSTNAME"
  IP_NAME="gojira-ingress-ip"
  gcloud compute addresses describe "$IP_NAME" --global --project "$PROJECT" >/dev/null 2>&1 \
    || gcloud compute addresses create "$IP_NAME" --global --project "$PROJECT"
  STATIC_IP="$(gcloud compute addresses describe "$IP_NAME" --global --project "$PROJECT" --format='value(address)')"

  # IAP OAuth client. Brands are one-per-project; API-created brands are
  # manageable here, Console-created ones are not — in that case create a
  # client by hand and pre-create the secret (we skip if it already exists).
  if ! kubectl -n gojira get secret gojira-iap-oauth >/dev/null 2>&1; then
    BRAND="$(gcloud iap oauth-brands list --project "$PROJECT" --format='value(name)' 2>/dev/null | head -1)"
    if [ -z "$BRAND" ]; then
      BRAND="$(gcloud iap oauth-brands create --project "$PROJECT" \
        --application_title="Gojira" \
        --support_email="$(gcloud config get-value account 2>/dev/null)" \
        --format='value(name)')"
    fi
    CLIENT_LINE="$(gcloud iap oauth-clients create "$BRAND" --display_name="gojira" \
      --format='value(name,secret)')"
    CLIENT_ID="$(basename "$(echo "$CLIENT_LINE" | cut -f1)")"
    CLIENT_SECRET="$(echo "$CLIENT_LINE" | cut -f2)"
    kubectl -n gojira create secret generic gojira-iap-oauth \
      --from-literal=client_id="$CLIENT_ID" \
      --from-literal=client_secret="$CLIENT_SECRET"
  fi

  TMP_ING="$(mktemp -d)"
  cp -r "$REPO_ROOT/deploy/ingress/." "$TMP_ING/"
  sed -i.bak "s#REPLACE_HOSTNAME#${GOJIRA_HOSTNAME}#" "$TMP_ING"/*.yaml
  sed -i.bak "s#REPLACE_STATIC_IP_NAME#${IP_NAME}#" "$TMP_ING"/*.yaml
  rm -f "$TMP_ING"/*.bak
  kubectl apply -k "$TMP_ING"
  rm -rf "$TMP_ING"

  # Who may pass IAP. App-level allowlisting still applies (auth: config).
  for member in $(echo "${GOJIRA_IAP_MEMBERS:-}" | tr ',' ' '); do
    gcloud iap web add-iam-policy-binding --project "$PROJECT" \
      --member="$member" --role=roles/iap.httpsResourceAccessor >/dev/null \
      && echo "iap access: $member"
  done

  cat <<EOI

front door provisioning (cert takes ~15-60 min after DNS resolves):
  1. Create an A record: ${GOJIRA_HOSTNAME} -> ${STATIC_IP}
  2. Watch: kubectl -n gojira get managedcertificate gojira-cert
  3. Enable app auth in your config.yaml:  auth: { enabled: true, allowed_domain: ... }
  4. Point your GitHub App webhook at https://${GOJIRA_HOSTNAME}/webhook/github
EOI
fi

cat <<EOF

Gojira is running. Reach the UI:

  kubectl -n gojira port-forward svc/gojira 8080:80
  open http://localhost:8080

Then register your cluster (Clusters page):
  name: sandbox   gcp_project: ${PROJECT}   location: ${REGION}   gke_cluster: ${CLUSTER}

and connect a repo (Connect page). Full walkthrough: docs/dogfood.md
EOF
