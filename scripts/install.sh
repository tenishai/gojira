#!/usr/bin/env bash
# The one entry point: install Gojira into a cluster from gojira.env.
#
#   cp gojira.env.example gojira.env   # fill it in
#   make install                       # or: ./scripts/install.sh
#
# Order: values → the platform underneath → bootstrap.sh (which runs
# preflight.sh first). Everything is idempotent. The platform underneath is
# one of:
#   GOJIRA_TERRAFORM=1        terraform/ creates the cluster and Gojira's
#                             identity (scripts/tf.sh; never deletes anything)
#   GOJIRA_PREPARE_PROJECT=1  prepare-project.sh grants Gojira's identity
#                             into a project whose cluster already exists
#   neither                   you ran terraform apply yourself, maybe elsewhere
# `make uninstall` is the way back.

set -euo pipefail
REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
. "$REPO_ROOT/scripts/env.sh"

if [ ! -f "${GOJIRA_ENV:-$REPO_ROOT/gojira.env}" ] && [ -z "${PROJECT:-}" ]; then
  echo "error: no values file: set GOJIRA_ENV=<path to your filled-in gojira.env>, or cp gojira.env.example gojira.env in this clone and fill it in" >&2
  exit 1
fi

PROJECT="${PROJECT:?set PROJECT in gojira.env (or the environment)}"
REGION="${REGION:-us-central1}"
CLUSTER="${CLUSTER:-gojira-sandbox}"

if [ "${GOJIRA_TERRAFORM:-}" = "1" ] && [ "${GOJIRA_PREPARE_PROJECT:-}" = "1" ]; then
  echo "error: GOJIRA_TERRAFORM and GOJIRA_PREPARE_PROJECT are both set." >&2
  echo "       terraform creates a cluster; prepare-project uses yours. Pick one." >&2
  exit 1
fi
if [ "${GOJIRA_TERRAFORM:-}" = "1" ]; then
  . "$REPO_ROOT/scripts/tf.sh"
  printf '\n\033[1m== terraform: cluster %s and the platform identity in %s\033[0m\n' "$CLUSTER" "$PROJECT"
  tf_apply
fi
if [ "${GOJIRA_PREPARE_PROJECT:-}" = "1" ]; then
  "$REPO_ROOT/scripts/prepare-project.sh"
fi
exec "$REPO_ROOT/scripts/bootstrap.sh"
