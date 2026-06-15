# Gojira — Environments & Multi-Project Deploys

**Status:** Design for review (2026-06-13)
**Authors:** Shaz + Ember (SRE AI)

Supersedes the phase-1 assumption that an app maps to a single cluster +
namespace. This is the design for promoting one app across `dev → qa → prod`
where each environment is its **own cluster**, optionally in its **own GCP
project**.

---

## Decision

An environment is a distinct **cluster + namespace**, never just a namespace
in a shared cluster. Environments may live in **the same project** (simplest)
or **different projects** (full prod isolation: separate IAM, billing,
blast radius). Both are supported by one model; same-project is just the case
where the distinct-project count is one.

Namespaces-in-one-cluster is explicitly rejected — it shares a control plane
and node pool, weakening both isolation and independent node scaling.

---

## Data model

```
environments
  id, app_id (fk), name (dev|qa|prod), cluster_id (fk), namespace
  unique (app_id, name)
```

`cluster_id` and `namespace` move OFF `App` and onto `Environment`. An App
keeps what's environment-independent: repo, workload class. Each environment
binds a name to a (cluster, namespace); the cluster carries its own
`gcp_project`, so the set of projects an app touches is derived:

```
projects(app) = { env.cluster.gcp_project for env in app.environments }
```

---

## Three provisioning scopes

The flat phase-1 sequence splits by scope. Each ledger row's `step_name` is
scoped so it stays unique under `(app_id, step_name)`:

| Scope | Steps | Runs | Ledger key |
|---|---|---|---|
| **Per-project** (identity) | apis, service account, WIF pool, WIF provider, bind repo, IAM roles, artifact registry | once per **distinct project** | `ensure_service_account@<project>` |
| **Per-environment** (cluster) | namespace | once per **environment** | `ensure_namespace@<env>` |
| **Per-app** (repo) | clone, generate, open PR | once | `open_setup_pr` |

The identity scope is the important change: a separate deploy GSA + WIF
binding + Artifact Registry live in **each** project. The same repo
principalSet is bound to each project's GSA, so one GitHub repo can deploy
into several projects, each grant explicit and per-project.

Same-project apps hit each per-project step once — the multi-project machinery
costs nothing in the common case.

---

## Images across projects — build through qa, copy qa→prod on promote

> **Revised (2026-06-14):** dev and qa each **build from scratch**; only prod
> is a crane-copy. Prod runs the exact bytes qa built and validated — you never
> promote an untested dev artifact straight to prod. (The earlier "build once
> in dev, copy to qa and prod" is superseded.)

- **CI (dev):** build the image, push to the **dev project's** Artifact
  Registry, deploy dev. (push to main)
- **Promote — qa:** build from scratch, push to **qa's** Artifact Registry,
  deploy qa. (on `v*` tag) qa exercises a real build of the tagged commit.
- **Promote — prod (gated):** `crane copy` the *exact* qa image into **prod's**
  Artifact Registry — no rebuild — then deploy. When prod shares qa's project
  (same registry) the image is already present, so the copy is skipped and prod
  deploys it as-is. Each cluster pulls from its **own** project's registry.

This keeps prod independent of dev's registry at deploy time (the point of
project isolation) and guarantees prod ships the artifact qa verified.

Generalized in code as a per-env `copy_from`: the first env (dev) is the CI
target, middle envs build from scratch, and the **last** env crane-copies from
its predecessor and is the gated production target. The cross-project
`artifactregistry.reader` grant the copy needs is provisioned only when the
prod and qa projects differ — a no-op for same-project apps.

---

## Generator changes

`GeneratorSpec` goes from one cluster/identity to a **list of environments**,
each carrying its own `{project, cluster, location, ar_repo, wif_provider,
gsa_email, namespace}`. Then:

- **Overlay per env** targets that env's namespace (already per-env dirs;
  now they also differ by cluster).
- **CI workflow** authenticates with the dev env's project WIF + GSA, pushes
  to dev's AR, deploys to dev's cluster.
- **Promote workflow** has, per downstream env, an auth block for *that env's*
  project, a `crane copy` into that env's AR, and a deploy to that env's
  cluster. Prod stays behind the `production` environment gate.

---

## Build phases

1. **Model**: add `Environment`; migrate `App` (drop cluster_id/namespace).
   Update the connect flow to accept an env→(cluster, namespace) map.
2. **Provisioner**: scope the sequence (per-project / per-env / per-app);
   scoped ledger step names; the orchestrator derives distinct projects and
   environments and drives the engine across them.
3. **Generator**: multi-env `GeneratorSpec`; per-env auth + crane-copy promote
   workflow; golden tests for a 2-project app.
4. **Validate**: provision a demo app with dev on `shaz-test` and a second
   cluster (same project first, then a second project) and watch promote
   crane-copy + deploy.

---

## Open sub-decisions

1. ~~**Cross-project AR reader vs crane-copy.**~~ **Resolved (2026-06-14):**
   crane-copy qa→prod. dev and qa build from scratch (no shared pull between
   them); prod crane-copies the qa-tested image. The only cross-project grant
   is the prod GSA reading qa's registry to perform the copy, provisioned
   per-env and dormant when both live in one project. Deploy-time pulls stay
   fully isolated — each cluster pulls its own project's registry.
2. **Same GSA name across projects?** Yes — `<app>-deploy@<each-project>`.
   Uniform, derivable, no central registry of names.
3. **Who registers the env→cluster map?** The connect flow (UI/API). For v1,
   a sensible default: all envs on one registered cluster's project unless the
   operator maps specific clusters per env.
