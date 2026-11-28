# Changelog

All notable changes to Gojira are documented here. The format loosely follows
[Keep a Changelog](https://keepachangelog.com/); versions are `vX.Y.Z` tags on
`main` (see RELEASING.md). Entries before v1.0.0 are in the git log.

## [Unreleased]

### Fixed

- **A merge GitHub refuses waits for a person; a failed call fails the step**
  (#144). A branch ruleset that wants a review or a check answers the setup
  PR's merge with 405. `merge_setup_pr` used to fail on that, leaving the app
  in `failed` beside a green, mergeable PR nobody was pointed at (a deploy
  repo in a public org, 2026-11-27). Now the PR stays open and the app waits
  in `awaiting_pr`, where every caller and the sync already know what to do;
  only a 5xx or a dead network fails the step, on a handed-over repo too.
  `GitHubError.status` carries the HTTP status from the App client.

### Changed

- **The runner variable is Gojira's, and it leaves when unwanted** (#155).
  Generated workflows read `vars.GOJIRA_CI_RUNNER` instead of the plain
  `CI_RUNNER` a repo's own CI might also read. `set_repo_variables` writes it
  only for a private repo while the install names a runner, removes it when
  either stops being true, and removes an old `CI_RUNNER` that still holds
  the value this install wrote. A public repo's workflows say `ubuntu-latest`
  outright. Installs that named a runner before: provision each repo once on
  this version *before* emptying `GOJIRA_CI_RUNNER`; that Provision
  regenerates the workflows and moves the variable over, and removes the old
  `CI_RUNNER` only while it still equals the runner the install names. When
  the repo's visibility cannot be read, the step treats it as private, on
  purpose.

- **Licence: MIT.** Relicensed from AGPL-3.0-or-later with every contributor's
  agreement (tenishai/agora discussion 69). Releases already published keep the
  licence they shipped under. (#148)
- **CI runs on GitHub-hosted runners only**, with a `ci` summary job a branch
  ruleset can require; it fails unless every job succeeded. (#151)
- **Releases arrive by pull request.** `make release` runs twice: the first run
  opens a release PR from `release/vX.Y.Z`, the second tags the merged commit.
  Nothing pushes to `main`. (#150)
- Community files for a public repo: CODE_OF_CONDUCT, SUPPORT, RELEASING,
  issue and pull request templates, this changelog, CODEOWNERS.

### Fixed

- The last client name on `main`, a shell comment in `scripts/prepare-project.sh`. (#147)
- `docs/install.md` linked the secrets store under its old name. (#153)
- Two stale sentences: the licence in `docs/design.md`, "no releases" in `SECURITY.md`. (#149)

## [v1.3.1] - 2026-11-22

### Fixed

- A self-hosted runner needs only docker: the generated workflow fetches kubectl
  and kustomize itself, and the contract check falls back from pip to apt. (#146)

## [v1.3.0] - 2026-11-18

### Added

- A cluster says how its control plane is reached: `access: dns | internal`.
  Generated CI uses `use_internal_ip` for an internal cluster, and the install
  skips the DNS-endpoint step for it. (#145)

## [v1.2.0] - 2026-11-18

### Added

- `namespace_labels:` in the contract: a label the app needs on its namespace,
  applied on every provision. (#143)
- A repo its owner handed over (`handed_over` with auto-merge) is merged without
  a person. (#142)

## [v1.1.0] - 2026-11-15

### Added

- A caller can seed the first setup PR, so a connected repo is asked once. (#140)

### Fixed

- `make release` re-anchors the repo card with the version bump, so the card
  check stays green on a release commit. (#139)

## [v1.0.0] - 2026-11-13

The first tagged release. Since the previous untagged state:

### Added

- `image:` in the contract deploys a published image; the generated CI copies
  it into the install's registry instead of building. (#122)
- A build arg may reference a repository variable the install sets. (#114)
- Settings and bundles land without a rebuild: optional Secret references, and
  CI builds only when the Dockerfile changed. (#132) `secret_mounts` may be
  optional. (#135) A contract that gains `identity:` on a live app takes
  effect. (#134)
- `observe:` speaks noto; clusters may name where noto lives. (#128)
- Gojira's own image is tagged with the commit and pinned by digest. (#137)
  Releases: a `vX.Y.Z` tag names every build and `/healthz` reports it. (#138)
- The front door signs in through Google's managed IAP client. (#136)

### Changed

- Every dependency is pinned in `uv.lock`; CI, `make dev` and the image install
  exactly that set. (#129)
- The generated CI passes `AR_TOKEN` as a BuildKit secret, not a build arg. (#109)

### Fixed

- Every overlay is built before the setup PR opens. (#111) A semantically
  equal `gojira.yaml` stays byte-for-byte on regeneration. (#112) Overlays and
  workflows for environments an app no longer has are removed. (#113)
- Uninstall finishes a failed app teardown under `--purge`, keeps a transcript,
  passes over a pool it already deleted, and deletes an image index before the
  manifests it lists. (#116, #120, #125)
- The env loader refuses a value with a trailing comment, naming the key. (#126)
  `prepare-project` waits for a just-created account before granting it roles. (#127)
