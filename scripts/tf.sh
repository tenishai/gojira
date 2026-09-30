# Sourced by install.sh and uninstall.sh: terraform driven by gojira.env.
#
# The values terraform needs are the ones gojira.env already holds, passed as
# TF_VAR_*, so there is no second file to keep in step. A terraform.tfvars
# that names a different project would win over them silently; refuse it.
# Auth is the gcloud identity every other step uses, as a short-lived token,
# so there is no second login either.

TF_DIR="${TF_DIR:-$REPO_ROOT/terraform}"

tf_env() {
  command -v terraform >/dev/null || { echo "error: terraform not installed" >&2; return 1; }
  if [ -f "$TF_DIR/terraform.tfvars" ] \
     && ! grep -q -E "^project_id *= *\"$PROJECT\"" "$TF_DIR/terraform.tfvars"; then
    echo "error: $TF_DIR/terraform.tfvars names another project than PROJECT=$PROJECT." >&2
    echo "       delete it (gojira.env is enough) or make the two agree." >&2
    return 1
  fi
  export TF_VAR_project_id="$PROJECT" TF_VAR_region="$REGION" TF_VAR_cluster_name="$CLUSTER"
  export GOOGLE_OAUTH_ACCESS_TOKEN="${GOOGLE_OAUTH_ACCESS_TOKEN:-$(gcloud auth print-access-token)}"
}

# Create or update the cluster and Gojira's identity. `make install` is rerun
# freely, so this must never be the command that quietly replaces a cluster:
# a plan that deletes anything stops here and is left for a person to read.
tf_apply() {
  tf_env || return 1
  local plan deletes
  plan="$(mktemp)"
  terraform -chdir="$TF_DIR" init -input=false >/dev/null
  terraform -chdir="$TF_DIR" plan -input=false -out="$plan" >/dev/null
  deletes="$(terraform -chdir="$TF_DIR" show -json "$plan" | python3 -c '
import json, sys
changes = json.load(sys.stdin).get("resource_changes", [])
print("\n".join(c["address"] for c in changes if "delete" in c["change"]["actions"]))')"
  if [ -n "$deletes" ]; then
    rm -f "$plan"
    echo "error: terraform wants to delete or replace:" >&2
    printf '         %s\n' $deletes >&2
    echo "       not doing that from make install. Review it yourself:" >&2
    echo "         terraform -chdir=$TF_DIR plan" >&2
    return 1
  fi
  terraform -chdir="$TF_DIR" apply -input=false "$plan"
  rm -f "$plan"
}

tf_destroy() {
  tf_env || return 1
  terraform -chdir="$TF_DIR" destroy -auto-approve -input=false
}

# True when terraform/ holds state for this project — terraform built it,
# here, whatever gojira.env says today.
tf_owns_project() {
  [ -f "$TF_DIR/terraform.tfstate" ] && grep -q "\"project\": \"$PROJECT\"" "$TF_DIR/terraform.tfstate"
}
