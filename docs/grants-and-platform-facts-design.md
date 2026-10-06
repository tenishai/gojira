# Grants and platform facts — design

*Status: approved 2026-10-06 (Shaz), open questions resolved as proposed
below. Covers issues #86 (cluster-scoped access), #87 (platform facts in
tenant repos) and the `gcp_roles` idea from the kobo and litellm installs.
Build order §3 → §1 → §2, one PR each.*

## The rule these three share

A tenant repo says **what the app needs**. The platform supplies **what the
platform knows**. Today three kinds of thing cross that line:

| Thing | Where it is today | Who had to do it |
|---|---|---|
| The pod's GCP identity (a GSA, its roles, the Workload Identity binding, the KSA annotation) | made by hand; the annotation in an app-authored overlay patch | the operator, per app (kobo-room, litellm) |
| Cluster-scoped access an app needs (a ClusterRole for its KSA) | in the app's kustomize base, where CI as the app identity cannot create it (#86) | the operator, by hand, after a failed deploy |
| The install's own facts: image path, WIF provider, deploy GSA, cluster name | written into the generated workflow and overlays (#87) | Gojira, into someone else's repo |

The first two are needs the contract cannot express. The third is the
platform writing itself into a repo. Three changes, one principle.

## 1. `identity:` — the pod's GCP identity — built 2026-10-06

```yaml
service_account: kobo              # the KSA, as today (default: the app name)
identity:
  gcp_roles: [roles/aiplatform.user, roles/storage.objectAdmin]   # on the environment's project
```

Gojira already does this dance for the **CI deploy identity** per project
(`EnsureServiceAccount` → `BindWifRepo` → `GrantIamRoles`, `steps.py`). The
same steps, different principal:

| | CI deploy identity (today) | Pod identity (new) |
|---|---|---|
| GSA | `<app>-deploy@<project>` | `<app>-run@<project>` |
| Principal bound | GitHub OIDC via WIF (`attribute.repository/<repo>`) | the KSA via Workload Identity: `<project>.svc.id.goog[<namespace>/<service_account>]` |
| Roles | fixed (`DEPLOY_ROLES`) | from the contract, each one in the operator's allowlist |
| In the repo | the workflow's `service_account:` | the rendered `k8s/base/serviceaccount.yaml`, name only: the annotation's value is an email naming a project, so CI applies it from `GOJIRA_<ENV>_RUN_SA` (§3's rule, found while building) |
| Teardown | app delete | app delete (purge drops the GSA) |

Rules:

- **Allowlist, enforced.** `iam.grantable_roles` in Gojira's config
  (`GOJIRA_GRANTABLE_ROLES`, comma list). A role not on it fails the
  provision with the role named — this is a security boundary, so it is
  not the overrides-style social `justification`. Empty list = the
  vocabulary is off and `identity:` is refused.
- **One GSA per app per project**, not per environment: two environments in
  one project share it (as the deploy identity does); environments in
  different projects each get their own.
- **Adopt what exists.** `identity.gsa: kobo-room@shaz-learning.iam.gserviceaccount.com`
  names a hand-made GSA instead of `<app>-run@`; Gojira grants/binds/annotates
  but never creates or deletes it. This is how kobo-room and litellm move
  under management without a cutover.
- **The KSA becomes generated** when `identity:` is present (today
  `service_account:` only *names* an app-authored object). An existing
  app-authored `serviceaccount.yaml` is replaced by the generated one; the
  setup PR shows the diff.
- Ledger rows per step, like every other grant; `delete_app` reverses them
  in the identity loop that already exists.

## 2. `cluster_roles:` — cluster-scoped access

```yaml
cluster_roles: [kobo-room]          # ClusterRoles the operator defined; Gojira binds the KSA to them
```

The deploy identity cannot create cluster-scoped RBAC and must not be able
to (#86). So the **ClusterRole is the operator's**, applied once as part of
the platform (a list of manifests under `deploy/cluster-roles/`, applied by
bootstrap, or any ClusterRole already on the cluster such as `view`), and the
**binding is Gojira's**: a per-environment step creates
`ClusterRoleBinding gojira-<app>-<role>` from the app's KSA to that role,
with Gojira's own identity (which holds `container.admin` since finding #11).

- Allowlist again: `iam.grantable_cluster_roles`. A name not on it fails the
  provision; a name on it that is missing from the cluster fails with
  "apply it first".
- The generated CI never sees a cluster-scoped kind. `generate_artifacts`
  refuses a base whose render contains one, naming the file (closes the
  "twenty retries" half of #86; #83 adds the overlay build).
- kobo's `k8s/cluster/rbac.yaml` splits: the ClusterRole moves to the
  dogfood's platform config; the binding line becomes `cluster_roles: [kobo-room]`.

## 3. Platform facts out of the repo (#87)

Only `CI_RUNNER` is a variable today. Generalise that:

| Fact | Today | Proposed |
|---|---|---|
| image path | `IMAGE:` in the workflow + `images[].newName` in the overlay | repo variable `GOJIRA_<ENV>_AR_REPO`; workflow composes `IMAGE`; overlay keeps only `name: gojira-app` and the deploy step's existing `kustomize edit set image` sets name and tag together |
| WIF provider, deploy GSA | literals in the workflow | `GOJIRA_<ENV>_WIF_PROVIDER`, `GOJIRA_<ENV>_DEPLOY_SA` |
| the pod's GSA annotation | an app-authored overlay patch | gone: §1 renders it |
| `environments[].cluster` | required | optional; the install's cluster for that environment name (`defaults.environment_clusters`, `{dev: gojira-sandbox}`) |

- `SetCiRunner` becomes `SetRepoVariables`, one step, all of them (the App
  has Variables RW since #82). Repo-level, not org-level or GitHub
  Environments: private repos on GitHub Free see neither (#81's lesson).
- A workflow run that finds a variable unset fails in its first step with
  "this repo is not connected to a Gojira that set `GOJIRA_DEV_AR_REPO`" —
  the honest failure for a fork or a second install that hasn't connected.
- After this a repo has **no diff between installs**: the contract and
  generated files are identical everywhere; every per-install value lives in
  that install's variables and that install's DB. kobo's repo stops carrying
  `shaz-learning`; the next regenerate cleans the three apps that do today.

## What stays in the repo, by design

`gojira.yaml` (the app's needs, including which roles it wants — a reviewer
should see that), `k8s/base` (app-authored namespaced objects), the
generated manifests and workflow, the `k8s/overlays/<env>` structure and any
drift-capture patches (cluster-authoritative state is still state of *this*
app). Secrets, ClusterRoles and GSAs never were and still aren't.

## Order and size

1. §3 first — no new permissions, removes the thing already bothering us
   (#87); generator + templates + `SetRepoVariables`; ~1 day.
2. §1 — the identity steps + allowlist + rendered KSA; adopt kobo-room and
   litellm; ~1.5 days.
3. §2 — the binding step + base refusal; kobo's ClusterRole into platform
   config; ~0.5 day.

Each is its own PR with a dogfood pass; none changes a Fubo install until
its operator sets the new knobs (all default off/empty).

## Open questions — resolved 2026-10-06

- Should `identity.gcp_roles` be allowed to name roles on a *different*
  project (cross-project reads, the Clio pattern)? **No** in the first cut;
  `project:` on a role entry later.
- `<app>-run@`: **kept**. GSA ids need 6–30 chars; a 2–3 letter app name
  needs the suffix anyway.
- Do we want the setup PR to list the grants it will make in its body, so
  the reviewer of the contract sees the consequence? Proposal: yes, cheap.
