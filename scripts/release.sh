#!/usr/bin/env bash
# Cut a Gojira release: a vX.Y.Z tag on main, with the version bumped in the
# package first and a GitHub release behind it.
#
#   make release VERSION=v1.2.0     # or: ./scripts/release.sh v1.2.0
#
# What it does, in order:
#   1. refuses unless on main, with no tracked changes, at origin/main, and
#      the tag does not exist yet
#   2. bumps pyproject.toml, gojira/__init__.py and uv.lock (the lock records
#      the project's own version; `uv sync --locked` would fail otherwise),
#      and the repo card (ARCHITECTURE.md anchors pyproject.toml and names
#      the version; CI's card check fails a bump that leaves it behind — the
#      v1.0.0 release did, 2026-11-13)
#   3. commits that (signed off), tags it, pushes main and the tag
#   4. publishes the GitHub release with generated notes
# Installs that pull main afterwards build gojira:vX.Y.Z@sha256:<digest>
# (scripts/bootstrap.sh names the image after the nearest tag).
set -euo pipefail
REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_ROOT"

VERSION="${1:?usage: scripts/release.sh vX.Y.Z   (make release VERSION=vX.Y.Z)}"
[[ "$VERSION" =~ ^v[0-9]+\.[0-9]+\.[0-9]+$ ]] || { echo "error: version must look like v1.2.0 (got $VERSION)" >&2; exit 2; }
BARE="${VERSION#v}"

for tool in git gh; do
  command -v "$tool" >/dev/null || { echo "error: $tool not installed" >&2; exit 1; }
done

branch="$(git rev-parse --abbrev-ref HEAD)"
[ "$branch" = "main" ] || { echo "error: release from main (this is $branch)" >&2; exit 2; }
[ -z "$(git status --porcelain --untracked-files=no)" ] \
  || { echo "error: tracked files have changes; commit or stash them first" >&2; exit 2; }
git fetch --quiet --tags origin
[ "$(git rev-parse HEAD)" = "$(git rev-parse origin/main)" ] \
  || { echo "error: main is not at origin/main; pull (or push your merge) first" >&2; exit 2; }
if git rev-parse -q --verify "refs/tags/$VERSION" >/dev/null; then
  echo "error: $VERSION already exists" >&2; exit 2
fi

sed -i.bak -E "s/^version = \"[^\"]+\"/version = \"$BARE\"/" pyproject.toml
sed -i.bak -E "s/^__version__ = \"[^\"]+\"/__version__ = \"$BARE\"/" gojira/__init__.py
rm -f pyproject.toml.bak gojira/__init__.py.bak
grep -q "^version = \"$BARE\"" pyproject.toml && grep -q "^__version__ = \"$BARE\"" gojira/__init__.py \
  || { echo "error: could not set the version lines" >&2; exit 1; }
files=(pyproject.toml gojira/__init__.py)
if [ -f uv.lock ]; then
  command -v uv >/dev/null || { echo "error: uv not installed (uv.lock records the version)" >&2; exit 1; }
  uv lock --quiet
  files+=(uv.lock)
fi

if [ -f ARCHITECTURE.md ]; then
  card_sha="$(git hash-object pyproject.toml)"
  sed -i.bak -E \
    -e "s/^(anchor: pyproject\.toml sha=)[0-9a-f]+$/\1$card_sha/" \
    -e "s/^updated: .*/updated: $(date -u +%F)/" \
    -e "s/^(STATE: main, )v[0-9]+\.[0-9]+\.[0-9]+/\1$VERSION/" ARCHITECTURE.md
  rm -f ARCHITECTURE.md.bak
  files+=(ARCHITECTURE.md)
  if [ -f scripts/check_arch_cards.py ] && ! python3 scripts/check_arch_cards.py >/dev/null; then
    echo "error: the architecture cards are not fresh after the bump; see scripts/check_arch_cards.py" >&2; exit 1
  fi
fi

git add "${files[@]}"
git commit --quiet --signoff -m "release: $VERSION"
git tag -a "$VERSION" -m "Gojira $VERSION"
git push --quiet origin main "$VERSION"
gh release create "$VERSION" --title "Gojira $VERSION" --generate-notes
echo "released $VERSION — installs that pull main now build gojira:$VERSION@sha256:<digest>"
