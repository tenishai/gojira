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

## Images across projects — build once, copy on promote

This is the ripple multi-project forces, and it's why the original design's
`crane copy` promotion exists:

- **CI (dev):** build the image once, push to the **dev project's** Artifact
  Registry. Deploy dev.
- **Promote (qa, then prod behind the gate):** `crane copy` (or
  `gcloud artifacts docker tags add` within a project) the *exact* dev image
  into the **target env project's** Artifact Registry — no rebuild — then
  deploy. Each cluster pulls from its **own** project's registry.

This keeps prod independent of dev's registry at deploy time (the point of
project isolation). For same-project apps, the copy is a tag within one
registry — cheap and uniform. The artifact promoted is identical bytes across
environments: build once, verify once, ship the same image everywhere.

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

1. **Cross-project AR reader vs crane-copy.** Recommended: **crane-copy per
   project** (above) — full isolation, matches the original promote design.
   The alternative (one shared AR, grant each cluster's pull identity
   `artifactregistry.reader` cross-project) is fewer moving parts but couples
   prod to the registry project. Lean: crane-copy.
2. **Same GSA name across projects?** Yes — `<app>-deploy@<each-project>`.
   Uniform, derivable, no central registry of names.
3. **Who registers the env→cluster map?** The connect flow (UI/API). For v1,
   a sensible default: all envs on one registered cluster's project unless the
   operator maps specific clusters per env.
