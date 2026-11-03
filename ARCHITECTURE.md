---
card: repo-card:gojira
updated: 2026-10-07
anchor: pyproject.toml sha=607f25b8197a5ce310994cb7e2ebd27839ab537f
anchor: gojira/web/__main__.py sha=0aaa920c9cc2a9f82f65d9477eafc654a0a038d5
---
# gojira — repo card

WHAT: opinionated deploy platform for GKE. Connects a GitHub repo, provisions
GCP identity/CI/manifests, deploys, watches for cluster drift (captures it
back to git as a PR), and ingests usage evidence to open rightsizing PRs.
STATE: main, v0.1.0, working and actively dogfooded; under active development
(a teammate has work in progress in this worktree's sibling checkout).
STACK: Python >=3.11 (PyPI name `tenish-gojira`, import name `gojira`), FastAPI + Jinja2/htmx web UI, SQLAlchemy (SQLite
default, Postgres via `[postgres]` extra + `DATABASE_URL`), PyYAML, GitHub App
auth (PyJWT RS256), `gcloud`/`kubectl` shelled out to (no GCP/K8s SDKs),
prometheus-client, OTel tracing (env-gated). `pip install -e .`; Docker
(`make` builds `--platform linux/amd64`; an app's own arch comes from `defaults.arch` or its contract's `arch:`); Terraform for the sandbox
cluster+identity (`terraform/`).
ENTRY:
- web server: `gojira/web/__main__.py` -> `gojira.web.create_app()` (FastAPI
  factory, `gojira/web/__init__.py`), `python -m gojira.web`
- service layer (non-FastAPI orchestration): `gojira/service.py`
- background loops started from create_app: drift scanner
  (`service.start_drift_scanner`, opt-in via `config.drift.enabled`)
SEAMS → card:
- provisioner-steps  gojira/provisioner.py, gojira/steps.py  (idempotent scoped step engine — the Deploy loop) → docs/arch/provisioner-steps.md
- drift-capture  gojira/drift.py, gojira/drift_scan.py  (allowlist-projection semantic diff — the Capture loop) → docs/arch/drift-capture.md
- rightsize-ingest  gojira/sizing/service.py, recommender.py, profile.py  (evidence ingest + recommendation — the Rightsize loop; cross-repo contract with gojira-sizing-agent) → docs/arch/rightsize-ingest.md
- auth  gojira/web/auth.py, gojira/config.py  (three independent auth mechanisms, do not conflate them) → docs/arch/auth.md
- observe-noto  gojira/steps.py (ObserveApp), gojira/config.py (ObserveConfig), gojira/web/metrics.py  (cross-repo contract with sibling repo noto, formerly argus) → docs/arch/observe-noto.md
- (uncarded) gojira/generator/__init__.py — Jinja2 render of CI workflows + Kustomize
  manifests from opinions+config. `preserve_patches`/`preserve_resources`/`preserve_generators`
  carry operator- and drift-authored kustomization entries through a re-render (findings
  #12, #18); `contract_fingerprint`/`stamp_contract` stop a gojira.yaml push and its setup
  PR landing as two separate deploys. `secrets_from_optional` renders Secret references the
  pod reads when present (`optional: true`), and the generated CI skips a
  build only for a Dockerfile that reads nothing from the build context (no COPY, ADD or bind
  mount): the registry keeps each such build under `build-<Dockerfile blob sha>` and a push whose
  Dockerfile matches re-tags that digest; a context-reading Dockerfile always builds, and a mutable
  base tag in FROM is refreshed only by a Dockerfile change or Redeploy (tenishai/kobo#434).
  Golden-file tested (tests/test_generator.py).
- (uncarded) gojira/github.py — `GitHubClient` ABC: `GhCLI` (shells to `gh`/`git`, laptop
  mode) and `GitHubAppClient` (RS256 JWT -> installation token, hosted mode).
- (uncarded) gojira/gcp.py, gojira/k8s.py — `GCPClient`/`KubectlClient` ABCs, same
  shell-out-behind-an-interface pattern as github.py.
- (uncarded) gojira/webhooks.py — GitHub webhook ingestion (HMAC-verified), the push
  counterpart to service.py's poll-based sync.
- (uncarded) gojira/opinions/__init__.py + opinions/sizing.yaml — the workload-class table
  (web/worker/cron/heavy/gpu/agent/gateway) the generator renders from.
DEPS: tenishai/gojira-sizing-agent — HTTP contract only, no import either direction (see
rightsize-ingest card); gojira/sizing/agent_deploy.py deploys that repo's manifests into a
registered cluster. tenishai/noto (formerly argus) — cross-repo contract for in-cluster `observe` Jobs and
the `/metrics` RED-series names noto's web profile scrapes (see observe-noto card). No
Python import dependency on any sibling repo.
HEALTH: 476 test functions across 38 files under tests/ (`make test` / `pytest -q`,
`pythonpath=["."]` in pyproject.toml so `tests.fakes` resolves under bare `pytest`).
CI (.github/workflows/ci.yaml): three jobs, on `vars.CI_RUNNER` when set (fork PRs always on ubuntu-latest) — `test` (ruff + pytest, py3.12), `scripts`
(bash -n on scripts/*.sh, `scripts/check_arch_cards.py`, `kubectl kustomize deploy/` and `deploy/ingress/` must render —
guards against the install path and deploy/ drifting apart, per tests/test_deploy_contract.py),
`terraform` (init + validate). tenishai repos have no branch protection; CI reports, never
blocks a merge. ruff select is explicit (E4,E7,E9,F) so the rule set doesn't move under a
ruff version bump.
GOTCHAS: "sizing" in this repo means container CPU/memory rightsizing, NOT the
gojira-sizing-agent repo's own release sizing — the two share a name prefix by design (the
agent is `gojira/sizing`'s producer), not by coincidence. observe/noto is non-halting by design
(`provision_app`'s observe pass, provisioner.py): it waits on the job, but a failed observe never
fails a provision. AuthConfig, the cluster-scoped agent bearer tokens, and `auth.api_token` are
three separate mechanisms guarding three different route families — see auth card, do not
assume one covers the others. `DNS1123_LABEL` validation in web/__init__.py is the only
gate on user-supplied names once `auto_merge_setup_pr` is set (finding #3) — auto-merge
removes the human-review backstop that used to catch this.
RELATED DOCS (reconcile, don't dupe): docs/design.md (original design, 2026-06-12 —
promotion model since revised, see the steps.py module docstring), docs/dogfood.md (live walkthrough),
docs/sizing-feedback-design.md (Phase 4 rightsizing design), docs/environments.md,
docs/github-app-setup.md.
