#!/usr/bin/env bash
# The one entry point: install Gojira into a cluster from gojira.env.
#
#   cp gojira.env.example gojira.env   # fill it in
#   make install                       # or: ./scripts/install.sh
#
# Order: values → (prepare-project.sh when GOJIRA_PREPARE_PROJECT=1) →
# bootstrap.sh (which runs preflight.sh first). Everything is idempotent.

set -euo pipefail
REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
. "$REPO_ROOT/scripts/env.sh"

if [ ! -f "${GOJIRA_ENV:-$REPO_ROOT/gojira.env}" ] && [ -z "${PROJECT:-}" ]; then
  echo "error: no gojira.env — cp gojira.env.example gojira.env and fill it in" >&2
  exit 1
fi

if [ "${GOJIRA_PREPARE_PROJECT:-}" = "1" ]; then
  "$REPO_ROOT/scripts/prepare-project.sh"
fi
exec "$REPO_ROOT/scripts/bootstrap.sh"
