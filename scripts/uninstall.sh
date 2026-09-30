#!/usr/bin/env bash
# Remove Gojira from a project — the reverse of `make install`, in the one
# order that works. Inputs come from gojira.env, as for the install.
#
#   make uninstall                      # asks before it touches anything
#   ./scripts/uninstall.sh --yes        # no question (automation)
#   ./scripts/uninstall.sh --purge      # also the things apps shared (below)
#   ./scripts/uninstall.sh --check      # change nothing; list what is left
#
# Three hands create things during an install: terraform (or
# prepare-project.sh), bootstrap.sh, and Gojira itself at run time. Only
# Gojira knows which apps it made identities for, and it keeps that record in
# the cluster — so the apps go first, while Gojira is still running, and the
# cluster goes last. `terraform destroy` alone forgets the rest.
#
# What it does, in order:
#   1. every connected app, through Gojira's own API: namespaces, deploy
#      service account, role grants, images
#   2. the front door, if there is one: ingress (which releases the load
#      balancer), certificate, static IP, IAP grants
#   3. Gojira's namespace (its database and disk go with it), the cluster-wide
#      objects it added, the sizing agent's monitoring grant
#   4. the platform itself: `terraform destroy` when terraform/ holds the
#      state for this project (cluster, image repo, service account); with
#      GOJIRA_PREPARE_PROJECT=1 the service account, its roles and its image
#      repo — and the cluster, which was never Gojira's, is left alone
#   5. (--purge) what every app shared: the identity pool `gojira`, the image
#      repo `apps` when empty, the Cloud Build bucket
#   6. a sweep of the project for anything of Gojira's still there
#
# Kept without --purge, and why: the identity pool (a deleted pool's name is
# reserved for 30 days; a reinstall restores it, but only Gojira knows how),
# the image repo `apps` and the Cloud Build bucket (other things may use
# them). Enabled APIs are never disabled.

set -euo pipefail
REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
. "$REPO_ROOT/scripts/env.sh"

PURGE=0; YES=0; CHECK=0
for arg in "$@"; do
  case "$arg" in
    --purge) PURGE=1 ;;
    --yes)   YES=1 ;;
    --check) CHECK=1 ;;
    *) echo "usage: $0 [--purge] [--yes] [--check]" >&2; exit 2 ;;
  esac
done

PROJECT="${PROJECT:?set PROJECT in gojira.env (or the environment)}"
REGION="${REGION:-us-central1}"
CLUSTER="${CLUSTER:-gojira-sandbox}"
GSA="gojira@${PROJECT}.iam.gserviceaccount.com"
. "$REPO_ROOT/scripts/tf.sh"
IP_NAME="gojira-ingress-ip"
POOL="gojira"
AGENT="gojira-sizing-agent"
BUCKET="${PROJECT}_cloudbuild"
PORT="${GOJIRA_UNINSTALL_PORT:-18099}"

step() { printf '\n\033[1m== %s\033[0m\n' "$*"; }
quiet() { "$@" >/dev/null 2>&1; }

for tool in gcloud kubectl curl python3; do
  command -v "$tool" >/dev/null || { echo "error: $tool not installed" >&2; exit 1; }
done

# Which hand built the platform decides who takes it down.
if tf_owns_project; then
  MODE=terraform
elif [ "${GOJIRA_PREPARE_PROJECT:-}" = "1" ]; then
  MODE=prepared
else
  MODE=unknown
fi

# GKE keeps volumes when a cluster is deleted. When the cluster was Gojira's
# (terraform), every volume it made is a leftover; on someone else's cluster
# only the ones claimed from namespace gojira are.
if [ "$MODE" = terraform ]; then
  DISK_FILTER="labels.goog-k8s-cluster-name=$CLUSTER AND description~created-for/pvc"
else
  DISK_FILTER='description~"pvc/namespace.:.gojira"'
fi

# --- 6. the sweep (also all of --check) ----------------------------------------
# Prints one line per thing found. "left" lines are failures of this script or
# of an earlier teardown; "kept" lines are the documented survivors.
sweep() {
  local left=0 line
  found() { printf '  %-5s %s\n' "$1" "$2"; [ "$1" = left ] && left=1; return 0; }
  kept_or_left() { if [ "$PURGE" = 1 ]; then found left "$1"; else found kept "$1"; fi; }

  while read -r line; do [ -n "$line" ] && found left "service account $line"; done < <(
    gcloud iam service-accounts list --project "$PROJECT" \
      --filter='displayName~^Gojira' --format='value(email)' 2>/dev/null)
  while read -r line; do [ -n "$line" ] && found left "role grant $line"; done < <(
    gcloud projects get-iam-policy "$PROJECT" --flatten='bindings[].members' \
      --format='value(bindings.role,bindings.members)' 2>/dev/null \
      | grep -E "(gojira|-deploy)@${PROJECT}\.iam|/sa/${AGENT}\$" | tr '\t' ' ' || true)
  while read -r line; do [ -n "$line" ] && found left "disk $line"; done < <(
    gcloud compute disks list --project "$PROJECT" --filter="$DISK_FILTER" \
      --format='value(name,zone.basename())' 2>/dev/null | tr '\t' ' ')
  quiet gcloud compute addresses describe "$IP_NAME" --global --project "$PROJECT" \
    && found left "static IP $IP_NAME"
  if [ "$MODE" != prepared ]; then
    quiet gcloud container clusters describe "$CLUSTER" --location "$REGION" --project "$PROJECT" \
      && found left "cluster $CLUSTER"
  fi
  quiet gcloud artifacts repositories describe gojira --location "$REGION" --project "$PROJECT" \
    && found left "image repo gojira"
  quiet gcloud artifacts repositories describe apps --location "$REGION" --project "$PROJECT" \
    && kept_or_left "image repo apps"
  [ "$(gcloud iam workload-identity-pools describe "$POOL" --location global \
        --project "$PROJECT" --format='value(state)' 2>/dev/null)" = ACTIVE ] \
    && kept_or_left "identity pool $POOL"
  quiet gcloud storage buckets describe "gs://$BUCKET" --project "$PROJECT" \
    && kept_or_left "bucket $BUCKET"
  return "$left"
}

if [ "$CHECK" = 1 ]; then
  step "what Gojira left in $PROJECT"
  if sweep; then echo "nothing unexpected."; exit 0; fi
  exit 1
fi

# --- the plan, and the question -------------------------------------------------
cat <<EOF
This removes Gojira from project $PROJECT:

  1. every connected app: its namespaces, deploy identity, role grants, images
  2. the front door, if there is one
  3. namespace gojira on cluster $CLUSTER, with Gojira's database
EOF
case "$MODE" in
  terraform) echo "  4. the cluster $CLUSTER itself, Gojira's image repo and service account (terraform destroy)" ;;
  prepared)  echo "  4. Gojira's service account, its roles, its image repo. Cluster $CLUSTER is not deleted." ;;
  unknown)   echo "  4. nothing: no terraform state for $PROJECT here. Run terraform destroy where the state lives." ;;
esac
if [ "$PURGE" = 1 ]; then
  echo "  5. identity pool $POOL, image repo apps (if empty), bucket $BUCKET"
else
  echo "  kept: identity pool $POOL, image repo apps, bucket $BUCKET (--purge removes them)"
fi
if [ "$YES" != 1 ]; then
  printf '\nType the project id to continue: '
  read -r answer
  [ "$answer" = "$PROJECT" ] || { echo "not removed."; exit 1; }
fi

FAILED=0
CLUSTER_UP=0
if quiet gcloud container clusters describe "$CLUSTER" --location "$REGION" --project "$PROJECT"; then
  CLUSTER_UP=1
  gcloud container clusters get-credentials "$CLUSTER" --region "$REGION" --project "$PROJECT" \
    ${GOJIRA_DNS_ENDPOINT:+--dns-endpoint} >/dev/null
fi

# --- 1. apps, while Gojira still knows them -------------------------------------
step "1/6 connected apps"
if [ "$CLUSTER_UP" = 1 ] && quiet kubectl -n gojira rollout status deploy/gojira --timeout=60s; then
  kubectl -n gojira port-forward svc/gojira "$PORT:80" >/dev/null 2>&1 &
  PF=$!
  trap 'kill "$PF" 2>/dev/null || true' EXIT
  for _ in $(seq 1 30); do quiet curl -sf "localhost:$PORT/healthz" && break; sleep 1; done
  TOKEN="$(kubectl -n gojira get secret gojira-github-app -o jsonpath='{.data.api_token}' | base64 -d)"
  api() { curl -s --max-time 600 -H "Authorization: Bearer $TOKEN" "$@"; }

  if ! APPS="$(api -f "localhost:$PORT/api/v1/apps")"; then
    echo "error: the running Gojira has no app list (older than this checkout)." >&2
    echo "       run 'make install' to bring it up to date, then uninstall again." >&2
    exit 1
  fi
  IDS="$(printf '%s' "$APPS" | python3 -c '
import json, sys
for a in json.load(sys.stdin)["apps"]:
    print(a["id"], a["name"])')"
  [ -n "$IDS" ] || echo "none connected"
  while read -r id name; do
    [ -n "$id" ] || continue
    echo "removing $name"
    api -X DELETE "localhost:$PORT/api/v1/apps/$id" | python3 -c '
import json, sys
s = json.load(sys.stdin)
i = s["identity"]
print("  namespaces:", ", ".join(s["namespaces_deleted"]) or "none",
      "| service account:", "deleted" if i["gsa_deleted"] else "not found",
      "| role grants:", i["bindings_removed"], "| images:", i["images_deleted"])
bad = [f["reason"] for f in s["namespace_failures"]] + i["failures"]
for b in bad:
    print("  FAILED:", b.splitlines()[0][:200])
sys.exit(1 if bad else 0)' || FAILED=1
  done <<< "$IDS"

  # Other clusters Gojira deployed to keep its sizing agent; say so.
  api "localhost:$PORT/api/v1/clusters" | CLUSTER="$CLUSTER" python3 -c '
import json, os, sys
for c in json.load(sys.stdin).get("clusters", []):
    if c["gke_cluster"] != os.environ["CLUSTER"]:
        print("  note: cluster %s (%s) may still hold namespace gojira and the sizing agent;"
              " remove them there with kubectl." % (c["gke_cluster"], c["gcp_project"]))' || true
  kill "$PF" 2>/dev/null || true
  trap - EXIT
else
  echo "Gojira is not running, so it cannot say which apps it created."
  echo "The sweep at the end lists what they left."
fi

if [ "$CLUSTER_UP" = 1 ]; then
  # --- 2. front door ------------------------------------------------------------
  step "2/6 front door"
  if [ -n "$(kubectl -n gojira get ingress -o name 2>/dev/null)" ]; then
    # Deleting the ingress is what releases the load balancer; a cluster
    # deleted with its ingress still in place orphans it.
    kubectl -n gojira delete ingress --all --timeout=300s
    kubectl -n gojira delete managedcertificate --all --ignore-not-found --timeout=120s
  else
    echo "none"
  fi

  # --- 3. Gojira's own namespace ---------------------------------------------
  step "3/6 namespace gojira and cluster-wide objects"
  # The namespace takes the claim with it, and the claim takes the disk: GKE
  # keeps disks when a cluster is deleted, so this has to happen first.
  kubectl delete namespace gojira --ignore-not-found --timeout=300s
  kubectl delete clusterrolebinding "$AGENT-read" --ignore-not-found
  kubectl delete clusterrole "$AGENT-read" --ignore-not-found
  kubectl delete priorityclass gojira-platform --ignore-not-found
  for _ in $(seq 1 30); do
    [ -z "$(kubectl get pv -o jsonpath='{range .items[?(@.spec.claimRef.namespace=="gojira")]}{.metadata.name}{"\n"}{end}' 2>/dev/null)" ] && break
    sleep 4
  done
else
  step "2/6 and 3/6: cluster $CLUSTER is already gone"
fi

if quiet gcloud compute addresses describe "$IP_NAME" --global --project "$PROJECT"; then
  gcloud compute addresses delete "$IP_NAME" --global --project "$PROJECT" --quiet
fi
for member in $(echo "${GOJIRA_IAP_MEMBERS:-}" | tr ',' ' '); do
  quiet gcloud iap web remove-iam-policy-binding --project "$PROJECT" \
    --member="$member" --role=roles/iap.httpsResourceAccessor || true
done
NUMBER="$(gcloud projects describe "$PROJECT" --format='value(projectNumber)')"
quiet gcloud projects remove-iam-policy-binding "$PROJECT" --role roles/monitoring.viewer --quiet \
  --member "principal://iam.googleapis.com/projects/${NUMBER}/locations/global/workloadIdentityPools/${PROJECT}.svc.id.goog/subject/ns/gojira/sa/${AGENT}" \
  || true

# --- 4. the platform ------------------------------------------------------------
step "4/6 the platform ($MODE)"
case "$MODE" in
  terraform)
    tf_destroy
    # Volumes of apps whose namespaces were still terminating outlive the
    # cluster. Nothing can mount them now.
    while read -r disk zone; do
      [ -n "$disk" ] || continue
      gcloud compute disks delete "$disk" --zone "$zone" --project "$PROJECT" --quiet
    done < <(gcloud compute disks list --project "$PROJECT" --filter="$DISK_FILTER" \
      --format='value(name,zone.basename())')
    CTX="gke_${PROJECT}_${REGION}_${CLUSTER}"
    quiet kubectl config delete-context "$CTX" || true
    quiet kubectl config delete-cluster "$CTX" || true
    quiet kubectl config delete-user "$CTX" || true
    ;;
  prepared)
    # Roles first: deleting the account first leaves "deleted:" members behind.
    while read -r role; do
      [ -n "$role" ] || continue
      gcloud projects remove-iam-policy-binding "$PROJECT" --member "serviceAccount:$GSA" \
        --role "$role" --condition=None --quiet >/dev/null && echo "revoked $role"
    done < <(gcloud projects get-iam-policy "$PROJECT" --flatten='bindings[].members' \
      --filter="bindings.members:serviceAccount:$GSA" --format='value(bindings.role)')
    quiet gcloud iam service-accounts describe "$GSA" --project "$PROJECT" \
      && gcloud iam service-accounts delete "$GSA" --project "$PROJECT" --quiet
    quiet gcloud artifacts repositories describe gojira --location "$REGION" --project "$PROJECT" \
      && gcloud artifacts repositories delete gojira --location "$REGION" --project "$PROJECT" --quiet
    ;;
  unknown)
    echo "skipped: run 'terraform destroy' where this project's state lives."
    ;;
esac

# --- 5. what the apps shared ----------------------------------------------------
step "5/6 shared resources"
if [ "$PURGE" = 1 ]; then
  quiet gcloud iam workload-identity-pools describe "$POOL" --location global --project "$PROJECT" \
    && gcloud iam workload-identity-pools delete "$POOL" --location global --project "$PROJECT" --quiet
  if quiet gcloud artifacts repositories describe apps --location "$REGION" --project "$PROJECT"; then
    if [ -z "$(gcloud artifacts docker images list "${REGION}-docker.pkg.dev/${PROJECT}/apps" \
          --format='value(package)' --limit 1 2>/dev/null)" ]; then
      gcloud artifacts repositories delete apps --location "$REGION" --project "$PROJECT" --quiet
    else
      echo "image repo apps still holds images that are not Gojira's apps; not deleted."
    fi
  fi
  quiet gcloud storage buckets describe "gs://$BUCKET" --project "$PROJECT" \
    && gcloud storage rm -r "gs://$BUCKET" --project "$PROJECT" --quiet
else
  echo "kept (see --purge)"
fi

# --- 6. prove it ----------------------------------------------------------------
step "6/6 what is left in $PROJECT"
if sweep && [ "$FAILED" = 0 ]; then
  printf '\nGojira is removed. The files its setup PRs added to your repos\n'
  printf '(gojira.yaml, k8s/, .github/workflows/gojira-ci.yaml) are yours to delete.\n'
else
  printf '\nGojira is NOT fully removed: see the "left" and FAILED lines above.\n' >&2
  exit 1
fi
