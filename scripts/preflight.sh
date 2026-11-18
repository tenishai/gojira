#!/usr/bin/env bash
# Preflight for a Gojira install: checks the operator machine, the GCP
# project, the cluster and the GitHub App inputs BEFORE bootstrap.sh spends
# ten minutes on a Cloud Build. Same env vars as bootstrap.sh; run it alone
# or let bootstrap run it as step 0.
#
#   PROJECT=my-project [REGION=us-central1] [CLUSTER=gojira-sandbox] \
#   [GOJIRA_GITHUB_APP_ID=…] [GOJIRA_GITHUB_APP_PEM=…] ./scripts/preflight.sh [--fix]
#
# --fix: enable DNS-based control-plane access on the cluster if it's off.
# Gojira's cluster registration probes the cluster through that endpoint
# (finding #14), so a cluster without it fails registration with an
# unhelpful gcloud error. Everything else is report-only.
#
# Exit 1 on any FAIL. WARNs never fail.

set -uo pipefail

FIX=0; [ "${1:-}" = "--fix" ] && FIX=1
. "$(cd "$(dirname "$0")" && pwd)/env.sh"
PROJECT="${PROJECT:?set PROJECT in gojira.env (or the environment)}"
REGION="${REGION:-us-central1}"
CLUSTER="${CLUSTER:-gojira-sandbox}"
fails=0
ok()   { printf '  \033[32mok\033[0m    %s\n' "$*"; }
warn() { printf '  \033[33mwarn\033[0m  %s\n' "$*"; }
fail() { printf '  \033[31mFAIL\033[0m  %s\n' "$*"; fails=$((fails + 1)); }

echo "preflight: $PROJECT / $REGION / $CLUSTER"

# --- tools ---------------------------------------------------------------
for tool in gcloud kubectl openssl; do
  if command -v "$tool" >/dev/null; then ok "$tool"; else fail "$tool not installed"; fi
done
if command -v gke-gcloud-auth-plugin >/dev/null; then
  ok "gke-gcloud-auth-plugin"
else
  warn "gke-gcloud-auth-plugin missing — kubectl can't auth to GKE from this machine (gcloud components install gke-gcloud-auth-plugin)"
fi
if gcloud container clusters get-credentials --help 2>/dev/null | grep -q -- '--dns-endpoint'; then
  ok "gcloud supports --dns-endpoint"
else
  fail "gcloud too old: no --dns-endpoint on get-credentials (gcloud components update)"
fi

# --- gcloud identity + project --------------------------------------------
ACCOUNT="$(gcloud config get-value account 2>/dev/null || true)"
if [ -n "$ACCOUNT" ]; then ok "gcloud account: $ACCOUNT"; else fail "no active gcloud account (gcloud auth login)"; fi
if gcloud projects describe "$PROJECT" --format='value(projectId)' >/dev/null 2>&1; then
  ok "project $PROJECT reachable"
else
  fail "cannot describe project $PROJECT as $ACCOUNT"
fi

# --- cluster ---------------------------------------------------------------
DNS="$(gcloud container clusters describe "$CLUSTER" --location "$REGION" --project "$PROJECT" \
  --format='value(controlPlaneEndpointsConfig.dnsEndpointConfig.allowExternalTraffic)' 2>/dev/null)"
rc=$?
if [ $rc -ne 0 ]; then
  fail "cluster $CLUSTER not found in $PROJECT/$REGION (terraform apply, or set CLUSTER/REGION)"
elif [ "${GOJIRA_CLUSTER_ACCESS:-}" = "internal" ]; then
  ok "cluster access internal: the DNS endpoint is not needed (register clusters with access internal)"
elif [ "$(echo "$DNS" | tr 'A-Z' 'a-z')" = "true" ]; then
  ok "cluster DNS-based access on"
elif [ $FIX -eq 1 ]; then
  if gcloud container clusters update "$CLUSTER" --location "$REGION" --project "$PROJECT" --enable-dns-access --quiet; then
    ok "cluster DNS-based access enabled (--fix)"
  else
    fail "could not enable DNS-based access on $CLUSTER"
  fi
else
  fail "cluster DNS-based access off — registration will fail. Rerun with --fix, or: gcloud container clusters update $CLUSTER --location $REGION --project $PROJECT --enable-dns-access"
fi

# --- GitHub App inputs (only when given) ------------------------------------
if [ -n "${GOJIRA_GITHUB_APP_ID:-}" ]; then
  case "$GOJIRA_GITHUB_APP_ID" in
    ''|*[!0-9]*) fail "GOJIRA_GITHUB_APP_ID must be the App's numeric id, got: $GOJIRA_GITHUB_APP_ID" ;;
    *) ok "GitHub App id $GOJIRA_GITHUB_APP_ID" ;;
  esac
else
  warn "GOJIRA_GITHUB_APP_ID not set (bootstrap requires it)"
fi
if [ -n "${GOJIRA_GITHUB_APP_PEM:-}" ]; then
  if [ ! -f "$GOJIRA_GITHUB_APP_PEM" ]; then
    fail "pem not found at $GOJIRA_GITHUB_APP_PEM"
  elif openssl pkey -in "$GOJIRA_GITHUB_APP_PEM" -noout >/dev/null 2>&1; then
    ok "pem parses as a private key"
  else
    fail "$GOJIRA_GITHUB_APP_PEM is not a readable private key (download the .pem from the App's settings page)"
  fi
else
  warn "GOJIRA_GITHUB_APP_PEM not set (bootstrap requires it)"
fi

echo
if [ $fails -gt 0 ]; then echo "preflight: $fails failure(s)"; exit 1; fi
echo "preflight: all clear"
