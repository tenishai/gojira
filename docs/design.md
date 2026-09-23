# Gojira — Design Document

**Status:** Approved — phase 1 in development
**Date:** 2026-06-12
**Authors:** Shaz + Ember (SRE AI)
**License:** AGPL-3.0-or-later

---

## What is Gojira

Gojira is an opinionated deploy platform for GKE. You connect a GitHub repo,
pick a target cluster and a workload class, and Gojira provisions everything
needed to ship: service account, Workload Identity Federation, Artifact
Registry access, GitHub Actions workflows, Kustomize manifests, and
observability wiring. After the setup PR merges, push-to-deploy just works.

The name is the thesis: Gojira is opinionated and tramples everything in its
path. It makes resource-sizing, health-check, scaling, and CI/CD decisions
*for* you, with an escape hatch for justified overrides. Fewer decisions,
fewer hand-rolled YAML files, fewer 4-CPU pods idling at 11% utilization.

**No ArgoCD.** Deploys go git → GitHub Actions → `kubectl apply` → cluster.
Drift goes the other way: cluster → drift detector → PR back to git (phase 2).
The cluster is authoritative during operations; git catches up through
reviewed PRs. Nothing silently reverts an operator's fix.

### Lineage

Gojira generalizes patterns proven on internal platforms:

- **An internal Cloud Run PaaS** — the "connect your repo" UX, GitHub
  App webhook handling, provisioner sequencing, build tracking.
- **argocd-cicd generator** (internal skill) — generation of GH Actions
  workflows + Kustomize base/overlays, minus the ArgoCD parts.
- **ATG case study** — production evidence that opinionated burstable sizing
  beats hand-rolled Guaranteed QoS (2 patches → 4x pod density).

This repo contains none of that internal code or data — only the patterns,
reimplemented cleanly.

---

## Decisions already made

| Decision | Choice | Rationale |
|---|---|---|
| Standalone vs extend existing platform | Standalone | Clean scope; open-sourceable; no coupling to internal systems |
| GitHub integration | GitHub App | Scoped permissions, installation flow, webhooks, can push commits/PRs. OAuth-only can't push cleanly |
| GCP auth (phase 1) | Operator's `gcloud` credentials | The v0.1 backend shells out to `gcloud`, so it uses the active `gcloud auth login` account — not ADC. (ADC matters only if we adopt the Python google-cloud SDKs.) Honest single-tenant story; hosted multi-tenant is out of scope |
| State store | SQLite default, Postgres optional | Zero-setup for evaluators; same SQLAlchemy models serve both |
| Language / stack | Python — FastAPI + SQLAlchemy + light server-rendered frontend (Jinja2 + htmx) | Fast to build, approachable for contributors, matches a shape proven on an internal PaaS |
| Cloud scope | GCP/GKE only | Coherent v1 pitch: "Gojira deploys to GKE." Multi-cloud is someone else's PR |

---

## Phase 1 scope — "connect your repo"

A user can:

1. Open Gojira's web UI, sign in via GitHub (App installation)
2. Connect a repo from the installation
3. Pick: target cluster (from Gojira's registered clusters), namespace,
   workload class (`web | worker | cron | heavy | gpu`), environment(s)
4. Gojira provisions GCP-side resources and opens a setup PR on the repo
5. Merge the PR → push to main → the app deploys to dev automatically
6. The UI shows the app's status: provision steps, setup PR, recent deploys

What Gojira provisions (GCP side, idempotent):

| Resource | Detail |
|---|---|
| Google Service Account | `<app>-deploy@<project>.iam.gserviceaccount.com` |
| Workload Identity Federation | Pool + GitHub OIDC provider (created once per project), plus a per-repo principalSet binding to the GSA |
| IAM roles on GSA | `roles/artifactregistry.writer`, `roles/container.developer` — the minimum to push images and `kubectl apply` |
| Artifact Registry repo | Created if absent (configurable: shared repo vs per-app) |
| Kubernetes namespace | Created if absent, labeled `gojira.dev/managed: "true"` |

What Gojira generates (committed via setup PR to the user's repo):

| Artifact | Path | Contents |
|---|---|---|
| CI workflow | `.github/workflows/gojira-ci.yaml` | Build → push to AR → `kubectl apply -k k8s/overlays/dev` on merge to main. WIF auth, no long-lived keys |
| Promote workflow | `.github/workflows/gojira-promote.yaml` | Tag-triggered: retag image, deploy QA, then deploy prod behind a manual approval gate (see Environment flow) |
| Kustomize base | `k8s/base/` | Deployment, Service, HPA (class-dependent), PDB |
| Overlays | `k8s/overlays/{dev,qa,prod}/` | Namespace, image tag, per-env resource patches |
| Observability wiring | in `k8s/base/` | Prometheus scrape annotations, `/metrics` port on Service, default PrometheusRules per class. Non-optional — a Gojira app is observable from its first deploy |
| Gojira spec | `gojira.yaml` | The app's declared class + overrides; see below |

### Environment flow

```
merge to main ──────────────► deploy dev        (automatic)
push tag vX.Y.Z ────────────► deploy qa         (automatic)
        └── same workflow ──► deploy prod       (manual approval gate)
```

The promote workflow runs on tag push: retags the already-built image
(`gcloud artifacts docker tags add` — no rebuild, the dev image is the
artifact), applies the QA overlay, then the prod job waits on a GitHub
**environment protection rule** (`environment: production` with required
reviewers) — a native Approve button in the Actions run.

Caveat: environment protection rules require a public repo or a paid GitHub
plan on private repos. The generator does **not** detect this: on a private
repo without them the `production` environment exists but has no required
reviewers, so the prod job runs ungated on merge. Check the environment's
protection rules after the setup PR merges. A `workflow_dispatch` fallback
for such plans is designed, not built.

### gojira.yaml — the app's contract

Lives in the repo root. Created by the setup PR, edited by the team afterward.
Gojira re-reads it on changes (webhook) and regenerates artifacts when the
class or overrides change.

```yaml
app: clips-api
class: web
cluster: my-dev-cluster          # must match a Gojira-registered cluster
namespace: clips
environments: [dev, qa, prod]
overrides:                       # optional; justification required
  requests: { cpu: "1", memory: "1Gi" }
  justification: "p99 CPU is 800m measured over 14 days"
```

### Workload classes — the opinion

| Class | Requests | Limits | HPA | Probes |
|---|---|---|---|---|
| `web` | 250m / 256Mi | 500m / 512Mi | 70% CPU, 2–10 | readiness + liveness HTTP `/health` |
| `worker` | 500m / 512Mi | 1 / 1Gi | — | liveness TCP/exec |
| `cron` | 250m / 256Mi | 1 / 1Gi | — | — |
| `heavy` | 1 / 1Gi | 2 / 2Gi | 70% CPU, 2–6 | readiness + liveness |
| `gpu` | 1 / 4Gi + 1 GPU | 2 / 8Gi + 1 GPU | — | liveness |

Principles:
- **Burstable QoS by default** (requests < limits, ratio 1:2). Production
  evidence shows Guaranteed QoS wastes nodes at low utilization.
- **Ephemeral storage always explicit** (`requests: 4Gi, limits: 8Gi`).
  Unset ephemeral specs are a hidden node-packing constraint.
- **Overrides need justification.** Free-text, in git, visible at review.
  Social enforcement in phase 1; admission enforcement (Kyverno) in phase 3.

---

## Architecture

```
┌─────────────────────────── Gojira (FastAPI) ───────────────────────────┐
│                                                                        │
│  UI (Jinja2 + htmx)      API (/api/v1)        Webhook (/webhook/github)│
│        │                      │                        │               │
│        └──────────┬───────────┴────────────────────────┘               │
│                   ▼                                                    │
│             Service layer                                              │
│   ┌──────────────┼──────────────────┬──────────────────┐               │
│   ▼              ▼                  ▼                  ▼               │
│ Provisioner   Generator         GitHub client      GCP client          │
│ (step engine) (templates →      (App auth, PRs,    (GSA, WIF, AR,      │
│               workflows,        commits, webhooks)  GKE via ADC)       │
│               kustomize)                                               │
│                   │                                                    │
│                   ▼                                                    │
│             SQLAlchemy ── SQLite (default) / Postgres (DATABASE_URL)   │
└────────────────────────────────────────────────────────────────────────┘
```

Single container. Runs anywhere `gcloud` has an identity — a laptop's
`gcloud auth login`, a VM, or in-cluster with Workload Identity.

### Data model (first cut)

```
clusters            registered deploy targets
  id, name, gcp_project, location, gke_cluster, ar_repo, created_at

apps                a connected repo
  id, name, repo_full_name, installation_id, cluster_id (fk),
  namespace, workload_class, status, setup_pr_url, created_at

provision_steps     audit + idempotency ledger, one row per step per app
  id, app_id (fk), step_name, status (pending|done|failed|skipped),
  resource_ref (e.g. GSA email), error, started_at, finished_at

events              append-only log: webhooks received, deploys observed,
  id, app_id (fk), type, payload_json, created_at
```

`provision_steps` is load-bearing: every provisioner step records what it
created, so re-runs are idempotent (skip `done` steps), failures are resumable
from the failed step, and teardown (`gojira destroy`, later) knows exactly
what to delete. Lesson learned from an internal PaaS's provisioner reliability work.

### Provisioner — step engine

Ordered, idempotent, resumable steps. Each step: `check()` (does it already
exist?) → `apply()` → record. A failed step halts the run, stores the error,
and surfaces in the UI with a retry button. No partial-state mystery.

Phase 1 step sequence:
1. `ensure_gsa`
2. `ensure_wif_pool_provider` (project-scoped, shared across apps)
3. `bind_wif_repo` (per-repo principalSet → GSA)
4. `grant_iam_roles`
5. `ensure_ar_repo`
6. `ensure_namespace`
7. `generate_artifacts` (render templates into a working tree)
8. `open_setup_pr` (branch + commit + PR via GitHub App)

### Configuration

`config.yaml` (operator-provided, with env-var overrides):

```yaml
github:
  app_id: ${GOJIRA_GITHUB_APP_ID}
  private_key_path: /secrets/github-app.pem
  webhook_secret: ${GOJIRA_WEBHOOK_SECRET}

database:
  url: sqlite:///gojira.db        # or postgresql://...

defaults:
  environments: [dev, qa, prod]
```

Clusters are registered through the UI/API (stored in DB), not in config —
they're data, not deployment configuration.

---

## Phase plan

| Phase | Ships | Definition of done |
|---|---|---|
| **1 — Connect** | GitHub App, web UI, provisioner, generator, setup PR, SQLite/Postgres | Empty repo → deployed on dev in < 10 min, no hand-written YAML |
| **2 — Capture** | Drift detector (central, scheduled in Gojira): live spec vs `kustomize build`, semantic diff on the managed-field projection, PR on drift. Deploy tracking in UI via webhook | SRE `kubectl patch` becomes a PR within 15 min, never reverted |
| **3 — Guard** | Kyverno policy pack (hard bounds enforce, soft recommendations audit), `gojira adopt` for existing services | Rogue 8-CPU pod rejected at admission; existing service adopted without spec changes |
| **4 — Adapt** | Adaptation engine v1: per-service thermostat (replica scaling + rightsizing from Prometheus), default HTTP synthetic probes, rate-of-change guardrails | Engine produces correct rightsizing PRs against real workloads |
| **Later** | Dependency-aware adaptation via pluggable topology providers, custom synthetic probes, predictive scaling, multi-cluster | — |

Phase 2's drift detector runs **centrally** in Gojira, not as a per-cluster
CronJob: drift's only data dependency is the K8s API, which Gojira already
reaches for every registered cluster (one kubectl context per cluster). A
central scheduler keeps the GitHub App credential in one place and reuses the
existing client seams. (Phase 4's sizing agent is the opposite case — it needs
in-cluster Prometheus, so it runs in-cluster and reports back.)

The adaptation engine's "nervous system" design (topology-graph bottleneck
tracing) depends on a topology source. Internally that's Cartographer; the
open-source core defines a `TopologyProvider` interface and ships without a
default implementation. Phase 4 works as a per-service thermostat without it.

---

## Open-source hygiene

- No internal hostnames, project IDs, IPs, or org names in code, tests, or
  docs. Examples use `example-project`, RFC 5737 IPs.
- All opinions (sizing table, IAM roles, workflow templates) live in a
  `gojira/opinions/` package — visible, documented, overridable. The README
  states them plainly: this is the point of the tool.
- Secrets via env/file only. No cloud-specific secret manager dependency in
  core.
- CI on the repo itself: pytest + ruff + a golden-file test suite for the
  generator (template output is the product — snapshot-test it).
- `Makefile` with `build` / `run` / `test` targets; Dockerfile. Postgres is a
  `DATABASE_URL` away (`pip install -e ".[postgres]"`); no compose file.

## Out of scope (explicitly)

- Hosted multi-tenant SaaS — Gojira is self-hosted by an operator with GCP
  credentials.
- Non-GKE targets (Cloud Run, EKS, plain VMs).
- Secret management for user apps (use ESO/your own tooling; Gojira won't
  touch app secrets).
- Build infrastructure — builds run in the user's GitHub Actions, not in
  Gojira.

## Resolved questions (2026-06-12)

1. **AR layout** — one shared Artifact Registry repo per project. Image
   path: `<location>-docker.pkg.dev/<project>/<ar-repo>/<app>:<tag>`.
2. **Environment flow** — merge to main → dev (auto), tag → qa (auto),
   prod behind a manual approval gate (GitHub environment protection,
   `workflow_dispatch` fallback). See Environment flow section.
3. **Dockerfile required** — phase 1 assumes a Dockerfile at repo root.
   Onboarding validates it exists and fails with a clear message if not.
   Buildpacks are a possible later fallback, not phase 1.
4. **Cluster registration validates connectivity** — registration runs a
   probe (`get-credentials` over the DNS endpoint, enabling it first if off) before accepting the
   cluster. A cluster Gojira can't reach is a registration error, not a
   deploy-time surprise.
5. **License** — AGPL-3.0-or-later (relicensed from the Apache-2.0 draft in PR #7).
