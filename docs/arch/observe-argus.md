---
card: arch-card:gojira-observe-argus
updated: 2026-09-28
anchor: gojira/steps.py sha=abc3da9be80359c19d0873e0203727af8b72aa8d
anchor: gojira/config.py sha=7e1a600090231522a81483f6ba4aab61a26f4634
anchor: gojira/web/metrics.py sha=77176d6ce0984db7bfb1f165a59f5299cd19b2a1
---
# observe-argus — the cross-repo contract with sibling repo argus

WHAT / SEAM: two contracts with tenishai/argus, co-designed
(steps.py:723-724, 2026-09-17/18):
  1. **Observe Job** — `ObserveApp` (steps.py, `ephemeral = True`) runs an
     in-cluster `argus observe` Job per (app, env) as the provisioner's last
     pass, parses one summary line back into the ledger.
  2. **RED metrics** — gojira/web/metrics.py exposes
     `http_requests_total{method,status}` and
     `http_request_duration_seconds_bucket{method}` at `/metrics` — "a panel
     contract, not a naming choice" (metrics.py:3-6): argus's web profile
     queries these exact series names.
OPS / FLOW: `ObserveApp.check()` SKIPS (not fails) when `config.observe.
job_image` is unset, the env's cluster isn't in `config.observe.clusters`,
or no `gojira.yaml` exists yet in the working tree — feature-off-when-absent.
`apply()` computes `contract_fingerprint(gojira.yaml)` (same canonicalization
generator.stamp_contract uses), builds a Job spec mirroring argus's own
README manifest, applies it, waits, reads logs, extracts the last
`ARGUS_SUMMARY ` line as the ledger's resource_ref.
INVARIANTS:
- `ephemeral = True`, runs in provisioner.py's dedicated NON-HALTING fourth
  pass (provisioner.py:262-281) — a failed/skipped observe never fails the
  provision or flips app.status. Always re-runs (no DONE short-circuit):
  argus's observe is itself idempotent and re-checks live state every run.
- Job `securityContext` pins `runAsUser=runAsGroup=fsGroup=10001`
  (`ARGUS_IMAGE_UID`, argus's Dockerfile uid) explicitly on the pod, not just
  the image — the kubelet writes the projected SA token 0600 root-owned
  otherwise. Getting this wrong reproduces a real outage (steps.py:816-820,
  first live run 2026-09-25): every observe failed with "platform record
  unreadable".
- Job name is `argus-observe-<epoch>-<env>-<app>`, truncated to 63 chars from
  the END (`.rstrip("-")`) — epoch stays intact so uniqueness against a
  still-TTLing completed Job survives even when the app name gets clipped
  (#49).
- `job_image` should be digest-pinned (config.py, "#37") — not enforced in
  code, an operator discipline note only.
- RED labels stay method+status, NEVER path (metrics.py:5-6) — a per-path
  label blows up argus's cardinality budget; this is argus's requirement.
- an unhandled handler exception still counts as a 500 in
  `http_requests_total` (metrics.py:47-54) — else a crashing app is invisible
  to the series argus alerts on (#44).
CONSUMERS: argus (sibling repo — its Job manifest and panel definitions are
NOT verified in this card; read argus's own docs directly).
gojira/provisioner.py's fourth pass calls `steps.observe_steps()`, which
includes `ObserveApp`.
API: `ObserveApp` (steps.py); `ObserveConfig`, `ObserveClusterConfig`
(config.py); `install(app)` (web/metrics.py, mounted once in
`gojira.web.create_app`).
