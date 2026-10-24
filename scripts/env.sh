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
    # Comments go on their own line. `KEY=1   # why` used to load the value
    # "1   # why", so a flag compared with `= 1` was silently off: a brownfield
    # install skipped preparing the project and Cloud Build failed pushing to a
    # registry that was never made (2026-10-23). Say which line, and stop.
    case "$_line" in *[[:space:]]'#'*)
      echo "error: $_gojira_env_file: '${_line%%=*}' has a comment after its value; put the comment on its own line" >&2
      return 1 2>/dev/null || exit 1 ;;
    esac
    _key="${_line%%=*}"
    [ -n "${!_key:-}" ] || export "$_line"
  done < "$_gojira_env_file"
fi
unset _gojira_env_file _line _key
