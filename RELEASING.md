# Releasing

A release is a `vX.Y.Z` tag on `main`, with the version bumped in the package
first and a GitHub release behind it. One command, run twice; the script does
the steps, this file is the checklist around it.

1. **Audit `[Unreleased]` in CHANGELOG.md against the log.** `git log --oneline
   v<previous>..HEAD`; every change a user would notice has an entry. Move the
   section under a `## [vX.Y.Z] - YYYY-MM-DD` heading in the same PR as the bump
   or just before it.
2. **First run:** from a clean checkout of `main` at `origin/main`,
   `make release VERSION=vX.Y.Z`. It bumps `pyproject.toml`, `gojira/__init__.py`
   and `uv.lock`, re-anchors the repo card, commits signed off on
   `release/vX.Y.Z`, pushes the branch and opens the release pull request.
   Nothing pushes to `main`.
3. **Merge the release PR** (squash). CI must be green: the card check fails a
   bump that left `ARCHITECTURE.md` behind (the v1.0.0 release did).
4. **Second run:** pull `main`, run the same command again. It sees the version
   on `main`, tags that commit, pushes the tag and publishes the GitHub release
   with generated notes. `git tag` and `gh release create` are separate acts;
   the script does both so neither is forgotten.
5. **Check the release page** lists the tag, and the notes read right. Add an
   `### Upgrading` paragraph by hand when an install must do something
   (a new `gojira.env` knob, a migration that needs a backup).
6. **Installs pick it up** with `make install` from a clone at the tag: the
   image is built as `gojira:vX.Y.Z@sha256:<digest>` and `/healthz` reports the
   version. Between releases an install names its build `vX.Y.Z-<n>-g<commit>`.

`scripts/release.sh` refuses to run off `main`, with tracked changes, behind
`origin/main`, for a tag that exists, or while a release PR is still open.
Tags are for maintainers (the `v*` ruleset); the release workflow of record is
this script, not CI.
