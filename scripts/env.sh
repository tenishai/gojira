# Sourced by every scripts/*.sh: load gojira.env (or $GOJIRA_ENV) so the
# install is driven by one values file instead of a dozen exports. A variable
# already set in the environment wins over the file, so
# `PROJECT=other ./scripts/bootstrap.sh` still works.
#
# File format: KEY=value per line, no quotes, # comments. Values may contain
# spaces (GOJIRA_NAMESPACE_LABELS={example.com/team: sre}).

_gojira_env_file="${GOJIRA_ENV:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/gojira.env}"
if [ -f "$_gojira_env_file" ]; then
  while IFS= read -r _line || [ -n "$_line" ]; do
    case "$_line" in ''|'#'*) continue ;; esac
    _key="${_line%%=*}"
    [ -n "${!_key:-}" ] || export "$_line"
  done < "$_gojira_env_file"
fi
unset _gojira_env_file _line _key
