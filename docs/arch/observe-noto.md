---
card: arch-card:gojira-observe-noto
updated: 2026-10-23
anchor: gojira/steps.py sha=6e5d2caefd8d85a1eeb5c8ea521603028be236f8
anchor: gojira/web/metrics.py sha=77176d6ce0984db7bfb1f165a59f5299cd19b2a1
---
# observe-noto — the cross-repo contract with sibling repo noto (named argus until 2026-10-12)

WHAT / SEAM: two contracts with tenishai/noto, co-designed
(`ObserveApp` docstring, 2026-09-17/18):
  1. **Observe Job** — `ObserveApp` (steps.py, `ephemeral = True`) runs an
     in-cluster `noto observe` Job per (app, env) as the provisioner's last
     pass, parses one summary line back into the ledger.
  2. **RED metrics** — gojira/web/metrics.py exposes
     `http_requests_total{method,status}` and
     `http_request_duration_seconds_bucket{method}` at `/metrics` — "a panel
     contract, not a naming choice" (metrics.py module docstring): noto's web profile
     queries these exact series names.
OPS / FLOW: `ObserveApp.check()` SKIPS (not fails) when `config.observe.
job_image` is unset, the env's cluster isn't in `config.observe.clusters`,
or no `gojira.yaml` exists yet in the working tree — feature-off-when-absent.
`apply()` computes `contract_fingerprint(gojira.yaml)` (same canonicalization
generator.stamp_contract uses), builds a Job spec mirroring noto's own
README manifest, applies it, waits, reads logs, extracts the last
`ARGUS_SUMMARY ` line as the ledger's resource_ref. The wire names `ARGUS_ENV` and
`ARGUS_SUMMARY ` are the contract and did not follow the rename.
INVARIANTS:
- `ephemeral = True`, runs in provisioner.py's dedicated NON-HALTING fourth
  pass (`provision_app`'s observe pass) — a failed/skipped observe never fails the
  provision or flips app.status. Always re-runs (no DONE short-circuit):
  noto's observe is itself idempotent and re-checks live state every run.
- Job `securityContext` pins `runAsUser=runAsGroup=fsGroup=10001`
  (`NOTO_IMAGE_UID`, noto's Dockerfile uid) on the pod, as hardening
  matching other token-reading workloads. It is NOT the fix for the first
  live run's (2026-09-25) "platform record unreadable": that theory (an
  unreadable projected token) was falsified; see the pin's commit ffbc55b
  and the comment in `_observe_job`.
- Job name is `noto-observe-<epoch>-<env>-<app>`, truncated to 63 chars from
  the END (`.rstrip("-")`) — epoch stays intact so uniqueness against a
  still-TTLing completed Job survives even when the app name gets clipped
  (#49).
- `job_image` should be digest-pinned (config.py, "#37") — not enforced in
  code, an operator discipline note only.
- RED labels stay method+status, NEVER path (metrics.py module docstring) — a per-path
  label blows up noto's cardinality budget; this is noto's requirement.
- an unhandled handler exception still counts as a 500 in
  `http_requests_total` (the `_record` middleware in `install`) — else a crashing app is invisible
  to the series noto alerts on (#44).
CONSUMERS: noto (sibling repo — its Job manifest and panel definitions are
NOT verified in this card; read noto's own docs directly).
gojira/provisioner.py's fourth pass calls `steps.observe_steps()`, which
includes `ObserveApp`.
API: `ObserveApp` (steps.py); `ObserveConfig`, `ObserveClusterConfig`
(config.py); `install(app)` (web/metrics.py, mounted once in
`gojira.web.create_app`).

CHANGE 2026-10-13: generate_artifacts builds every environment overlay with kustomize before the setup PR (#83); the generated CI builds once before its apply retries.
CHANGE 2026-10-13: generate_artifacts keeps a semantically equal gojira.yaml byte-for-byte, compared by contract fingerprint (#84).
CHANGE 2026-10-13: generate_artifacts prunes overlays and gojira-* workflows it wrote for environments the app no longer has; hand-written files are listed in the setup PR (#96).
CHANGE 2026-10-13: build_args may reference a repository variable as ${NAME}; CIConfig.repo_variables (GOJIRA_REPO_VARIABLES) names the values and SetRepoVariables sets them; an unknown reference refuses the provision (#102).
CHANGE 2026-10-15: generate_artifacts refuses a contract that names both `image:` and `build_args:` (#108); an `image:` contract renders a crane-copy CI step instead of a build.
CHANGE 2026-10-23: observe defaults follow the rename — namespace `noto`, service account `noto-observe`, Job name `noto-observe-*`; GOJIRA_OBSERVE_CLUSTERS entries may carry `name=namespace/service_account` so an install whose platform still lives in `argus` names it; the wire names ARGUS_ENV / ARGUS_SUMMARY are unchanged.
