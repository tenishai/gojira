---
card: arch-card:gojira-provisioner-steps
updated: 2026-11-27
anchor: gojira/steps.py sha=735d8137803320f4e300fbeb6d7e503fea243c79
---
# provisioner-steps — the idempotent scoped step engine (Deploy loop)

WHAT / SEAM: `Step` Protocol (provisioner.py) — `name: str`,
`check(ctx) -> str | None` (resource ref if it exists), `apply(ctx) -> str`
(create it, return its ref). `ProvisionEngine(steps).run(session, ctx,
scope_key=None) -> RunResult` walks one step list, recording every outcome in
the `provision_steps` ledger. `provision_app(session, app, *, gcp, github,
kubectl_for, config, workdir)` is the orchestrator: it
derives scopes from the app and drives the engine across them.
OPS / FLOW: `provision_app` runs four engine passes, each over its own
`ProvisionContext` (one scope field set per pass): (1) per-project identity —
once per distinct GCP project (`steps.per_project_steps()`: APIs, SA, WIF
pool/provider, repo binding, IAM roles, Artifact Registry); (2) per-env
cluster setup (`per_env_steps()`); (3) per-app repo flow — once, unscoped
(`per_app_steps()`: generator render, the run-identity and cluster-role grants the contract asks for, repo variables, setup PR, optional merge); (4) per-env observe
— AFTER (3), since it needs the contract in the working tree
(`observe_steps()`, see observe-noto card). Ledger rows key as
`{step.name}@{scope_key}`, so one step name run for several
projects/envs stays distinct. Passes (1)-(3) halt on the first FAILED step
(app -> FAILED); pass (4) never halts the run.
INVARIANTS:
- steps must be idempotent: the engine trusts `check()`'s "already exists"
  completely and never re-verifies `apply()`'s ref afterward.
- a DONE/SKIPPED ledger row is skipped on re-run UNLESS the step sets
  `ephemeral = True` (see the `Step` docstring) — for steps whose "resource"
  doesn't outlive one run. Wrong either way: skip a step that must re-run
  (observe), or re-run a step that should be a one-time cloud mutation.
- `ProvisionEngine.__init__` raises on duplicate step names in one sequence
  (`ProvisionEngine.__init__`) — ledger keys would otherwise collide.
- promotion order is deterministic (`order_environments`, config's env order
  then `Environment.id`) — the LAST env crane-copies its predecessor's image
  rather than rebuilding (steps.py module docstring; this REVISES docs/design.md's
  original promotion model — trust steps.py, not the design doc).
- environments are validated non-empty BEFORE any state mutation
  (`provision_app`, before `app.status` changes) — an app with none must never appear to have
  started a run.
- `ProvisionContext.adopted` is set by `GenerateArtifacts` only in adopt mode: the repo had an existing `k8s/base/deployment.yaml` and no `gojira.yaml` yet. For an adopted repo, `MergeSetupPr.check` refuses auto-merge (returns a reason, so the step is skipped and the app waits in `awaiting_pr`): a person wrote the repo, so a person reviews the PR (finding #22). The one exception is `App.handed_over`, which only a caller's own statement sets (`handed_over` on `POST /api/v1/apps`, or on `POST /api/v1/apps/{id}/provision` for an app connected earlier; never inferred): the owner gave the repo's deploy files to the platform, so the adopted PR is merged, a seed may ride an auto-merged PR, and a merge GitHub refuses (a branch rule) leaves the PR open and the app in `awaiting_pr` instead of failing the step. A repo with hand-written manifests AND a `gojira.yaml` is not adopted, so this check does not stop its setup PR from auto-merging.
- `ProvisionContext.spec` is the contract-applied spec `GenerateArtifacts` rendered from; later per-app steps (identity, `cluster_roles`, repo variables) read it via `_app_spec` rather than rebuilding a contract-less spec.
- a grant outside the install's allowlist (`iam.grantable_roles`, `iam.grantable_cluster_roles`) or a cluster-scoped object in `k8s/base` raises `IdentityRefused` and fails the pass; the allowlists are a security boundary, not a hint.
- `gojira.steps` is imported lazily inside `provision_app`
  (the `from gojira.steps import` lines in `provision_app`) to avoid a provisioner<->steps import cycle.
CONSUMERS: gojira/service.py drives `provision_app` from a background thread
behind the UI's "Provision" button; web/__init__.py's `/apps/{id}/provision`
and `/api/v1/apps/{id}/provision` routes trigger it through service.py.
API: `Step`, `ProvisionContext`, `ProvisionEngine`, `RunResult`,
`provision_app`, `order_environments`, `distinct_projects`
(provisioner.py); concrete steps and `per_project_steps()` / `per_env_steps()`
/ `per_app_steps()` / `observe_steps()` (steps.py).

CHANGE 2026-10-13: generate_artifacts builds every environment overlay with kustomize before the setup PR (#83); the generated CI builds once before its apply retries.
CHANGE 2026-10-13: generate_artifacts keeps a semantically equal gojira.yaml byte-for-byte, compared by contract fingerprint (#84).
CHANGE 2026-10-13: generate_artifacts prunes overlays and gojira-* workflows it wrote for environments the app no longer has; hand-written files are listed in the setup PR (#96).
CHANGE 2026-10-13: build_args may reference a repository variable as ${NAME}; CIConfig.repo_variables (GOJIRA_REPO_VARIABLES) names the values and SetRepoVariables sets them; an unknown reference refuses the provision (#102).
CHANGE 2026-10-15: generate_artifacts refuses a contract that names both `image:` and `build_args:` (#108); an `image:` contract renders a crane-copy CI step instead of a build.

CHANGE 2026-10-23: observe defaults and Job name follow the noto rename; GOJIRA_OBSERVE_CLUSTERS entries may name the namespace/service account per cluster.
CHANGE 2026-11-09: the identity steps, bind_cluster_roles and set_repo_variables carry `rechecks = True`: a DONE/SKIPPED ledger row no longer skips them, so a contract that gains `identity:` or `cluster_roles:` on a live app takes effect at the next provision (the engine honours `rechecks` beside `ephemeral`).
CHANGE 2026-11-14: `POST /api/v1/apps` takes `seed` (contract keys, a few `k8s/base` files, a note), kept on `App.setup_seed_json`; generate_artifacts applies it only to a repo with no `gojira.yaml` and no `k8s/` directory, sets `ProvisionContext.seed_needs` / `seed_files`, and the setup PR lists them. A seed is refused with `auto_merge_setup_pr`, and may set only the keys in `SEED_NEEDS` (never `identity` or `cluster_roles`, which the steps before the PR would grant unreviewed).
CHANGE 2026-11-15: a seed's needs are typed (`SeedNeeds`, strict: a wrong shape is refused, never coerced) and its files must be named core objects, one file per object; the seed cannot change once on the row, so what would fail every provision is refused at the door. `_rendered_targets` skips a document whose metadata is not a mapping.
CHANGE 2026-11-16: `App.handed_over` (set by the caller, with `auto_merge_setup_pr`): `MergeSetupPr` merges an adopted repo's setup PR, `ConnectIn` accepts a seed with auto-merge, the PR body says nobody is asked to review, and a refused merge waits in `awaiting_pr`. Without it nothing changes (finding #22 stands).
CHANGE 2026-11-17: `label_namespaces` (per-app, after `bind_cluster_roles`, `rechecks = True`) sets the contract's `namespace_labels:` on the app's namespace in every environment via `KubectlClient.label_namespace` (`kubectl label --overwrite`); `ensure_namespace` still creates the namespace with the install's labels only, since it runs before the contract is read. A key the install sets itself is refused with ValueError.
CHANGE 2026-11-27: `MergeSetupPr` leaves a merge GitHub refuses (405/409/422, or gh's sentence: a rule wants a review or a check) open for a person on any repo, the app in `awaiting_pr` with its `setup_pr_url`; a failed call (5xx, network) fails the step, handed-over or not (`github.merge_refused`, #144).
CHANGE 2026-11-18: `Cluster.access` (dns | internal): `ensure_cluster_dns_access` skips a cluster registered as internal (`rechecks = True` so a change of mode is seen at the next provision), `EnvSpec.access` renders `use_internal_ip: true` instead of `use_dns_based_endpoint: true` in both workflows, and `GCPClient.get_gke_credentials` takes the mode (`--internal-ip` / `--dns-endpoint`). Default dns = every cluster registered before.
CHANGE 2026-11-23: `set_repo_variables` owns `GOJIRA_CI_RUNNER` (was the plain `CI_RUNNER`) and now takes away what it no longer wants (issue #155): the runner variable when `ci.runner` is empty or the repo is public, and an old `CI_RUNNER` that still equals this install's runner. `GitHubClient` gained `delete_repo_variable` and `repo_is_public`; `build_spec` sets `GeneratorSpec.public_repo` from the latter (private when GitHub can't be asked), and the workflow templates render `runs-on: ubuntu-latest` outright for a public repo.
