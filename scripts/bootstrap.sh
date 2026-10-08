#!/usr/bin/env bash
# Bootstrap Gojira into a cluster — everything after `terraform apply` (or
# prepare-project.sh). Inputs come from gojira.env (see gojira.env.example);
# `make install` is the front door, this script is what it runs.
#
#   cp gojira.env.example gojira.env && $EDITOR gojira.env && make install
#   # or, values in the environment instead of the file:
#   PROJECT=my-sandbox GOJIRA_GITHUB_APP_ID=1234567 \
#   GOJIRA_GITHUB_APP_PEM=./github-app.pem ./scripts/bootstrap.sh
#
# Optional front door (IAP + HTTPS LB; see deploy/ingress/):
#   GOJIRA_HOSTNAME=gojira.example.com     enables the ingress step
#   GOJIRA_IAP_MEMBERS=domain:example.com  comma-separated IAP accessor grants
#   GOJIRA_ALLOWED_DOMAIN / GOJIRA_ALLOWED_EMAILS  Gojira's own allowlist —
#                                          auth: turns on only with a hostname
# After it provisions, point DNS (A record) at the printed static IP.
#
# What it does, in order (each step idempotent — rerun freely):
#   0. preflight                  (scripts/preflight.sh --fix: tools, project,
#                                  cluster DNS access, App id + pem)
#   1. cluster credentials        (gcloud get-credentials)
#   2. build + push the image     (Cloud Build — works from ARM machines too)
#   3. namespace + Gojira secret  (App id + pem you supply; webhook secret and
#                                  API token minted here, preserved on rerun)
#      + gojira-install ConfigMap (the optional knobs, as pod env — the
#                                  committed configmap.yaml interpolates them)
#   4. render + apply deploy/     (placeholders filled in a temp copy; your
#                                  working tree stays clean)
#   5. wait for the pod, print how to reach the UI + the minted secrets
#   6. (only with GOJIRA_HOSTNAME) static IP, IAP OAuth client, ingress
#
# It deliberately does NOT read terraform state: every value is derived from
# PROJECT/REGION and the conventions in terraform/ (GSA `gojira@<project>`,
# AR repo `gojira`, cluster `gojira-sandbox`), so it runs from any machine —
# terraform can live on a different one.

set -euo pipefail
REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
. "$REPO_ROOT/scripts/env.sh"

# --- inputs -------------------------------------------------------------------
PROJECT="${PROJECT:?set PROJECT in gojira.env (or the environment)}"
REGION="${REGION:-us-central1}"
CLUSTER="${CLUSTER:-gojira-sandbox}"
# NB: no apostrophes in :? messages — inside ${VAR:?...} a quote character
# starts a quote context even within double quotes and eats the script.
APP_ID="${GOJIRA_GITHUB_APP_ID:?set GOJIRA_GITHUB_APP_ID (the GitHub App numeric id)}"
PEM="${GOJIRA_GITHUB_APP_PEM:?set GOJIRA_GITHUB_APP_PEM (path to the App private key .pem)}"
[ -f "$PEM" ] || { echo "error: pem not found at $PEM" >&2; exit 1; }

AR_PATH="${REGION}-docker.pkg.dev/${PROJECT}/gojira"
GSA="gojira@${PROJECT}.iam.gserviceaccount.com"

step() { printf '\n\033[1m== %s\033[0m\n' "$*"; }

for tool in gcloud kubectl openssl; do
  command -v "$tool" >/dev/null || { echo "error: $tool not installed" >&2; exit 1; }
done

step "0/5 preflight"
PROJECT="$PROJECT" REGION="$REGION" CLUSTER="$CLUSTER" "$REPO_ROOT/scripts/preflight.sh" --fix

step "1/5 cluster credentials ($CLUSTER @ $PROJECT/$REGION)"
# GOJIRA_DNS_ENDPOINT=1: reach a private-endpoint cluster over GKE's
# IAM-authenticated DNS endpoint (brownfield clusters often have no public
# endpoint and a VPN-only private one; the operator machine may be on
# neither). Requires allowExternalTraffic on the cluster's dnsEndpointConfig.
gcloud container clusters get-credentials "$CLUSTER" --region "$REGION" --project "$PROJECT" \
  ${GOJIRA_DNS_ENDPOINT:+--dns-endpoint}

step "2/5 build + push image via Cloud Build (avoids the ARM/amd64 trap)"
gcloud services enable cloudbuild.googleapis.com --project "$PROJECT" --quiet
# A project that has never built before answers PERMISSION_DENIED — to its own
# owner — for about a minute after the API is enabled, while Google grants the
# build service its identity (first install into a fresh project, 2026-09-29).
# Wait and retry that one error; any other failure is the build's own.
BUILD_LOG="$(mktemp)"
for attempt in 1 2 3; do
  if gcloud builds submit --tag "${AR_PATH}/gojira:dev" --project "$PROJECT" "$REPO_ROOT" \
      2>&1 | tee "$BUILD_LOG"; then
    break
  fi
  if [ "$attempt" = 3 ] || ! grep -q PERMISSION_DENIED "$BUILD_LOG"; then
    rm -f "$BUILD_LOG"
    echo "error: Cloud Build failed" >&2; exit 1
  fi
  echo "Cloud Build is not ready in this project yet — waiting 45s (attempt $attempt of 3)"
  sleep 45
done
rm -f "$BUILD_LOG"

step "3/5 namespace + Gojira secret"
kubectl create namespace gojira --dry-run=client -o yaml | kubectl apply -f -
# Two secrets Gojira mints for you, preserved across reruns: the webhook
# secret (you paste it into the GitHub App) and the API token that gates the
# /api/v1 onboarding API (the pod refuses to boot without one — configmap.yaml).
# tests/test_deploy_contract.py keeps this list in step with deployment.yaml.
existing_key() { kubectl -n gojira get secret gojira-github-app -o jsonpath="{.data.$1}" 2>/dev/null | base64 -d || true; }
WEBHOOK_SECRET="$(existing_key webhook_secret)"; WEBHOOK_SECRET="${WEBHOOK_SECRET:-$(openssl rand -hex 32)}"
API_TOKEN="$(existing_key api_token)";           API_TOKEN="${API_TOKEN:-$(openssl rand -hex 32)}"
kubectl -n gojira create secret generic gojira-github-app \
  --from-literal=app_id="$APP_ID" \
  --from-literal=webhook_secret="$WEBHOOK_SECRET" \
  --from-literal=api_token="$API_TOKEN" \
  --from-file=private_key="$PEM" \
  --dry-run=client -o yaml | kubectl apply -f -

# The optional knobs, handed to the pod as env; configmap.yaml's ${VAR:-default}
# lines read them. Gojira's own allowlist only arms behind a front door — the
# identity header it trusts is forgeable on a bare endpoint.
AUTH_ENABLED=false
if [ -n "${GOJIRA_HOSTNAME:-}" ] && [ -n "${GOJIRA_ALLOWED_DOMAIN:-}${GOJIRA_ALLOWED_EMAILS:-}" ]; then
  AUTH_ENABLED=true
fi
kubectl -n gojira create configmap gojira-install \
  --from-literal=GOJIRA_AUTH_ENABLED="$AUTH_ENABLED" \
  --from-literal=GOJIRA_ALLOWED_DOMAIN="${GOJIRA_ALLOWED_DOMAIN:-}" \
  --from-literal=GOJIRA_ALLOWED_EMAILS="${GOJIRA_ALLOWED_EMAILS:-}" \
  --from-literal=GOJIRA_NAMESPACE_LABELS="${GOJIRA_NAMESPACE_LABELS:-}" \
  --from-literal=GOJIRA_DEFAULT_ARCH="${GOJIRA_DEFAULT_ARCH:-}" \
  --from-literal=GOJIRA_DEFAULT_NODE_SELECTOR="${GOJIRA_DEFAULT_NODE_SELECTOR:-}" \
  --from-literal=GOJIRA_ENVIRONMENT_CLUSTERS="${GOJIRA_ENVIRONMENT_CLUSTERS:-}" \
  --from-literal=GOJIRA_GRANTABLE_ROLES="${GOJIRA_GRANTABLE_ROLES:-}" \
  --from-literal=GOJIRA_GRANTABLE_CLUSTER_ROLES="${GOJIRA_GRANTABLE_CLUSTER_ROLES:-}" \
  --from-literal=GOJIRA_SIZING_AGENT_IMAGE="${GOJIRA_SIZING_AGENT_IMAGE:-}" \
  --from-literal=GOJIRA_OBSERVE_JOB_IMAGE="${GOJIRA_OBSERVE_JOB_IMAGE:-}" \
  --from-literal=GOJIRA_OBSERVE_CLUSTERS="${GOJIRA_OBSERVE_CLUSTERS:-}" \
  --from-literal=GOJIRA_DRIFT_ENABLED="${GOJIRA_DRIFT_ENABLED:-true}" \
  --from-literal=GOJIRA_CI_RUNNER="${GOJIRA_CI_RUNNER:-}" \
  --from-literal=GOJIRA_REPO_VARIABLES="${GOJIRA_REPO_VARIABLES:-}" \
  --dry-run=client -o yaml | kubectl apply -f -

step "4/5 render + apply manifests (temp copy — your tree stays clean)"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
cp -r "$REPO_ROOT/deploy/." "$TMP/"
sed -i.bak "s#REPLACE_GSA_EMAIL#${GSA}#" "$TMP/serviceaccount.yaml"
sed -i.bak "s#REPLACE_AR_PATH#${AR_PATH}#" "$TMP/kustomization.yaml"
rm -f "$TMP"/*.bak
kubectl apply -k "$TMP"

step "5/5 roll onto the image just built, then wait for Gojira to come up"
# Gojira's own image ships under the mutable :dev tag, so a rebuild with no
# manifest change is an apply no-op and the old pod keeps running the old
# image (#37's shape, on our own install — found 2026-09-25). Compare the
# digest the registry holds for the tag with the digest the pod is running
# and restart when they differ. `tags list` is pure Artifact Registry
# (describe pays Container Analysis's toll, #47).
# gcloud's tag: filter is a prefix match (tag:dev also lists dev-…, and
# other rows besides) — select the exact tag ourselves.
WANT="$(gcloud artifacts docker tags list "${AR_PATH}/gojira" --filter=tag:dev \
  --format='value(tag,version)' --project "$PROJECT" 2>/dev/null | awk '$1=="dev"{print $2; exit}')"
HAVE="$(kubectl -n gojira get pods -l app.kubernetes.io/name=gojira \
  -o jsonpath='{.items[0].status.containerStatuses[0].imageID}' 2>/dev/null | sed 's/.*@//')"
# No digest on the pod = nothing has run yet (first install): the pod being
# created pulls what was just built (imagePullPolicy: Always), so a restart
# would only kill it mid-pull.
if [ -n "$WANT" ] && [ -n "$HAVE" ] && [ "$WANT" != "$HAVE" ]; then
  echo "rolling: registry has ${WANT:0:19}, pod runs ${HAVE:0:19}"
  kubectl -n gojira rollout restart deploy/gojira
fi
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
  3. Gojira's allowlist: auth.enabled=${AUTH_ENABLED} (set GOJIRA_ALLOWED_DOMAIN or
     GOJIRA_ALLOWED_EMAILS in gojira.env and rerun to change it)
  4. Point your GitHub App webhook at https://${GOJIRA_HOSTNAME}/webhook/github
     and set its secret to the minted webhook secret (printed below).
EOI
fi

cat <<EOF

Gojira is running. Reach the UI:

  kubectl -n gojira port-forward svc/gojira 8080:80
  open http://localhost:8080

Then register your cluster (Clusters page):
  name: ${CLUSTER}   gcp_project: ${PROJECT}   location: ${REGION}   gke_cluster: ${CLUSTER}

and connect a repo (Connect page). Full walkthrough: docs/install.md

Secrets minted for you (stable across reruns):
  webhook secret — set it on the GitHub App's webhook (Settings → Webhook):
    kubectl -n gojira get secret gojira-github-app -o jsonpath='{.data.webhook_secret}' | base64 -d; echo
  API token — Authorization: Bearer <token> for the /api/v1 onboarding API:
    kubectl -n gojira get secret gojira-github-app -o jsonpath='{.data.api_token}' | base64 -d; echo
EOF
