# Installing Gojira

From an empty GCP project to an app Gojira deployed, measured and can
rightsize: about 40 minutes, most of it waiting for a cluster. Steps 1 to 8
were run in this order into a project that had never seen Gojira, then
removed and installed again (last run: 2026-09-29). Where the run surprised
us, the page says so.

You end with Gojira running in its own cluster under Workload Identity,
authenticating to GitHub as a GitHub App, and
[`examples/hello-app`](../examples/hello-app/README.md) deployed by it from a
repo of yours. Already have a cluster? Read
[Existing cluster](#existing-cluster) first.

---

## Before you start

| You need | Notes |
|---|---|
| A GCP project with billing | A throwaway. Gojira's identity gets broad IAM in it (it creates service accounts and grants roles), which is fine for a sandbox and nowhere else. |
| To be Owner on that project | Or hold project IAM admin, service usage admin, and the GKE, Artifact Registry and IAM admin roles. |
| `gcloud`, `kubectl`, `terraform`, `openssl`, `gh` | No Docker: images build in Cloud Build, so an ARM laptop is safe. |
| GitHub Actions runners for your app repos | GitHub's hosted runners need nothing. Your own runners need `docker`, `gcloud`, `kubectl` and `kustomize` on them; put their label in `GOJIRA_CI_RUNNER` and Gojira sets `CI_RUNNER` on every repo it connects (an org-level variable would not reach private repos on GitHub Free). An arm64 runner builds arm64 images: set `GOJIRA_DEFAULT_ARCH=arm64` (and how your clusters provide it, e.g. `GOJIRA_DEFAULT_NODE_SELECTOR={cloud.google.com/compute-class: Scale-Out}` on Autopilot) so every app is built and scheduled arm64 unless its `gojira.yaml` says otherwise. Prefer arm64 where both sides can: it is cheaper and more available than amd64. A GitHub-hosted `ubuntu-latest` runner cannot build arm64, so leave the default empty (amd64) with hosted runners. **Give the runner its own `CLOUDSDK_CONFIG` and `KUBECONFIG`** (environment variables in its service unit): the generated workflow signs gcloud in with the app's identity, and on a runner that shares a person's home directory that sign-in replaces the person's active account. |
| `gke-gcloud-auth-plugin` | `gcloud components install gke-gcloud-auth-plugin` |
| A GitHub App | Ten minutes: [github-app-setup.md](github-app-setup.md) §1–§3. You need its numeric **App ID** and its **private key** (`.pem`). |

```bash
git clone https://github.com/tenishai/gojira && cd gojira
gcloud auth login
gcloud config set project <your-project>
```

> **More than one Google account on this machine?** `kubectl` caches its
> token per cluster, not per account. A single `kubectl` call made under the
> wrong account is then reused for an hour by every later call, which fails
> with `User "<other account>" cannot get resource`. Give this install its
> own kubeconfig: `export KUBECONFIG=~/.kube-gojira/config`.

---

## 1. Fill in the values

```bash
cp gojira.env.example gojira.env
$EDITOR gojira.env
```

| Set | To |
|---|---|
| `PROJECT`, `REGION` | your project, and the region for the cluster |
| `GOJIRA_GITHUB_APP_ID`, `GOJIRA_GITHUB_APP_PEM` | the App's numeric id, the path to its key |
| `GOJIRA_TERRAFORM` | `1` |

## 2. Install (about 16 minutes the first time)

```bash
make install
```

With `GOJIRA_TERRAFORM=1` this first runs terraform: an Autopilot cluster
named `gojira-sandbox`, an image repo for Gojira's own image, and Gojira's
service account bound to its in-cluster identity (about 9 minutes). Terraform
reads its values from `gojira.env` and signs in as your `gcloud` account;
there is no second file and no second login. State stays on your machine in
`terraform/`, ignored by git. Keep it, and run `make uninstall` from this same
clone: the uninstall reads the state next to it, and a clone without it
cannot take the cluster down (it says so, and leaves the platform standing).

Then it checks your tools and the cluster, builds the image in Cloud Build,
creates the secret (your App key, plus a webhook secret and an API token it
mints), applies the manifests and waits for the pod.

The image is named after the checkout's nearest release tag (`v1.2.0`, or
`v1.2.0-3-g<commit>` when main has moved on since) and the pod runs it by
digest, the same receipt Gojira gives every app it deploys. The UI footer and
`/healthz` show the version; the closing message names the image, and so does
the cluster:

```bash
kubectl -n gojira get deploy gojira -o jsonpath='{.spec.template.spec.containers[0].image}'
# …/gojira/gojira:v1.2.0@sha256:<digest>
```

`:dev` is kept as a floating alias of the latest build. Releases are cut on
the repo with `make release VERSION=vX.Y.Z` (see CONTRIBUTING.md).

It is safe to rerun. Rerunning is also how you upgrade and how you change any
value in `gojira.env`. On a rerun terraform takes seconds, and it will not
delete or replace anything: if a change would, `make install` stops and
names what, for you to review.

In a project that has never used Cloud Build, the first build is refused for
about a minute after the API is enabled. The script waits and retries; you
will see `Cloud Build is not ready in this project yet`.

It finishes by printing how to open the UI and how to read the two secrets.

Prefer to run terraform yourself, or keep its state on another machine?
Leave `GOJIRA_TERRAFORM` empty and run `terraform apply` in `terraform/`
before `make install`.

## 3. Open the UI

```bash
kubectl -n gojira port-forward svc/gojira 8080:80
```

Browse to <http://localhost:8080>. Leave this running; the API calls below
use it too.

## 4. Register the cluster

**Clusters** → add:

| Field | Value |
|---|---|
| Name | `gojira-sandbox` |
| GCP project | your project |
| Location | `us-central1` (your `REGION`) |
| GKE cluster | `gojira-sandbox` |

Register a cluster under its real GKE name. Everything that later reads your
metrics, logs or CI refers to the cluster by that name, and a nickname chosen
here has to be translated forever.

Registration proves Gojira can reach the cluster before accepting it.

## 5. Put hello-app in a repo of yours

Gojira deploys repos, not directories.

```bash
cp -r examples/hello-app ~/hello-app && cd ~/hello-app
git init -q && git add -A && git commit -qm "hello-app" && git branch -M main
gh repo create <you>/hello-app --private --source=. --push
cd -
```

Then add the repo to your GitHub App's installation
([github-app-setup.md](github-app-setup.md) §3). Gojira cannot see a repo the
App is not installed on.

## 6. Connect it

**Connect repo** →

| Field | Value |
|---|---|
| App name | `hello-app` |
| Repo | `<you>/hello-app` |
| Workload class | `web` |
| dev — cluster | `gojira-sandbox` |
| dev — namespace | `hello` (empty = the app name) |

Or the same thing through the API:

```bash
TOKEN="$(kubectl -n gojira get secret gojira-github-app -o jsonpath='{.data.api_token}' | base64 -d)"
curl -s -X POST localhost:8080/api/v1/apps -H "Authorization: Bearer $TOKEN" \
  -H 'content-type: application/json' -d '{
    "name": "hello-app", "repo_full_name": "<you>/hello-app", "workload_class": "web",
    "environments": [{"name": "dev", "cluster": "gojira-sandbox", "namespace": "hello"}]}'
```

Gojira creates the app's deploy identity, the GitHub-to-GCP trust, an image
repo and the namespace, then opens a **setup PR** on your repo and waits for
you. The app page shows each step and links the PR.

A service that connects apps for other people (a portal, say) and needs a
little of its own in each one can ask for it in the same call, so the repo's
owner reviews one PR instead of two. `seed` is read only for a repo that has
neither a `gojira.yaml` nor a `k8s/` directory; a repo that already has either
is rendered from what is in it, and the seed is ignored.

```json
"seed": {
  "needs": {"service_account": "hello", "secrets_from_optional": ["hello-extra"]},
  "base_files": {"serviceaccount.yaml": "apiVersion: v1\nkind: ServiceAccount\nmetadata: {name: hello}\n"},
  "note": "One paragraph for the person who reviews the PR."
}
```

- `needs` are the keys of `gojira.yaml` for what the pod reads and mounts:
  `service_account`, `env`, `secrets_from`, `secrets_from_optional`,
  `config_from`, `data_mount`, `config_mounts` and `secret_mounts`, in the
  shape the file takes. They are written into the contract and rendered.
  `identity` and `cluster_roles` are not among them: Gojira grants those
  before the PR is opened, so they are a person's to write.
- `base_files` are added to `k8s/base/` as written and listed in its
  `kustomization.yaml`: at most 8 files of 16 KiB, each one object of kind
  `ServiceAccount`, `PersistentVolumeClaim` or `ConfigMap`. A name Gojira
  renders itself, or one already in the repo, is left alone.
- `note` is quoted in the setup PR, beside the list of what the seed added.

The call is refused with `422` when a key or a file is not one of those, when
a value is one the contract would not keep as written (the wrong shape, or a
name such as `on` that YAML reads as something else), and when `seed` comes
with `auto_merge_setup_pr`: a seed is reviewed by a person, unless the repo
was handed over (below).

A service that wrote the repo itself can pass `"auto_merge_setup_pr": true`,
and Gojira merges the setup PR without waiting. For a repo somebody else
wrote, that alone is not enough: a repo with its own `k8s/` keeps its PR open
for a person, and a seed is refused. `"handed_over": true` beside it is the
service's statement that the repo's owner gave its deploy files (`gojira.yaml`,
`k8s/`, the `gojira-*` workflows) to the platform. Gojira then merges its own
PRs there, the first one and every regeneration after it, and each PR says so.
Only say it when the owner agreed to exactly that where they connected the
repo: the first PR regenerates the `k8s/` they wrote. An app connected before
its owner agreed is handed over later with
`POST /api/v1/apps/<id>/provision` and the body `{"handed_over": true}`.
The repo is still the owner's on GitHub, so a branch rule that refuses the
merge wins: the PR stays open and the app waits for a person to merge it.

## 7. Merge the setup PR

Read it first: it adds `gojira.yaml` (the contract), `k8s/` and one workflow.
That is everything Gojira will ever ask your repo to hold. Merge it, then tell
Gojira (until you wire the webhook, it does not hear about merges):

- UI: **check PR merge** on the app page, or
- API: `curl -s -X POST localhost:8080/api/v1/apps/1/sync -H "Authorization: Bearer $TOKEN"`
  → `{"id":1,"status":"active","changed":true}`

The merge itself starts the deploy. Watch it and look at the result:

```bash
gh run watch -R <you>/hello-app          # about 2 minutes
kubectl -n hello get deploy,pods,hpa
kubectl -n hello port-forward svc/hello-app 8081:80 &
curl -s localhost:8081/health            # ok
```

You have an app that Gojira deployed, with no pipeline you wrote.

> **If the port-forward drops here**, run it again. The first app brings
> alert rules, which makes Google's managed Prometheus start a high-priority
> pod, and on a full node something has to make room. Gojira runs at a modest
> priority of its own so that it is not the first choice; if it is evicted
> anyway it is back in about 30 seconds with its state intact.

---

## 8. See drift captured

Someone patches production by hand at 3 a.m.:

```bash
# Both values: a request above the class's 500m limit alone is rejected
# by the API server (requests must be <= limits).
kubectl -n hello set resources deploy/hello-app --requests=cpu=750m --limits=cpu=1500m
```

App page → **Drift** → **Scan now**. You get findings (`requests.cpu`: git
250, cluster 750, in millicores; `limits.cpu`: 500, 1500) and a **capture PR**
that writes the live values into the environment's overlay. Merging it makes
git match the cluster. Gojira records what happened; it does not revert it.
With `GOJIRA_DRIFT_ENABLED=true` the same sweep runs every 15 minutes.

On Autopilot the scan also lists memory and ephemeral-storage. You did not
change those; Autopilot did, because it keeps memory in proportion to CPU and
sets storage limits itself. The capture PR records them too.

## 9. What next

- **A hostname and sign-in**: set `GOJIRA_HOSTNAME` and the allowlist in
  `gojira.env`, rerun `make install`, create the DNS record it prints.
- **The webhook**, so merges are heard without a button:
  [github-app-setup.md](github-app-setup.md) §4.
- **Rightsizing**: [sizing-feedback-design.md](sizing-feedback-design.md) and
  the README's "Feeding the rightsizing loop".
- **One repo, any install**: nothing of your install is written into a
  connected repo — registry, deploy identity and cluster reach its CI as
  repository variables Gojira sets (`GOJIRA_<ENV>_*`), and the same repo
  connected to another Gojira gets that install's values. Set
  `GOJIRA_ENVIRONMENT_CLUSTERS` so a connect request can say `dev` without
  naming your cluster. A build that needs a value from your install (a
  package index in your project) names it in the contract as
  `build_args: {EXTRA_INDEX_URL: ${GOJIRA_PYTHON_INDEX}}`, and you say what
  that is here in `GOJIRA_REPO_VARIABLES={GOJIRA_PYTHON_INDEX: https://…}`.
- **Apps that need a Google identity** (Vertex, a bucket): set
  `GOJIRA_GRANTABLE_ROLES` to the roles you are willing to grant, and an app
  says `identity: {gcp_roles: [roles/aiplatform.user]}` in its `gojira.yaml`.
  Gojira makes the account, grants, binds and annotates; the setup PR lists
  what it granted. Empty means no app may declare one.
- **Apps that need cluster-wide access** (a room that reads other
  namespaces): apply the ClusterRole yourself, once, put its name in
  `GOJIRA_GRANTABLE_CLUSTER_ROLES`, and the app says `cluster_roles:
  [that-name]`. Gojira binds the app's ServiceAccount to it on every
  environment's cluster. `k8s/base` must hold only namespaced objects.
- **Apps whose namespace must carry a label** (a secrets service that admits
  a caller by a label on its namespace): the app says `namespace_labels:
  { sero.dev/caller: "true" }` and Gojira sets it on the namespace in every
  environment, on every provision. The install's own labels
  (`GOJIRA_NAMESPACE_LABELS`, the managed label) are not an app's to change;
  a contract that names one is refused. A label the contract drops is left
  in place: remove it by hand if it must go.
- **Upgrading an install from before this** (October 2026): give the App
  the Variables permission first ([github-app-setup.md](github-app-setup.md)
  §1); existing apps keep their old workflows until you press **Provision**
  again on each, which sets the variables and then regenerates.
- **Your own app**: anything with a `Dockerfile` at its root. Port, health
  path and scaling bounds are yours to declare in `gojira.yaml`; a repo that
  already has `k8s/` manifests is adopted, not overwritten.
- **Software you did not write**: a repo holding only `gojira.yaml` with
  `image: <registry>/<path>:<tag>` and the `k8s/base` the upstream keeps.
  Nothing is built; the generated CI copies the published image into your
  registry and deploys it like any other app, and an upgrade is a tag bump
  in that one file. Your deploy identity must be able to read the source
  registry (a private Artifact Registry: `roles/artifactregistry.reader`).
- **Files beside the image** (`bundle_image: <registry>/<path>:<tag>`): an
  OCI image mounted read-only at `/bundle` as an image volume, so a change
  to those files is a publish and a restart, not a build. Needs Kubernetes
  1.35 or newer (image volumes are on by default from 1.35; an older cluster
  rejects the Deployment when it is applied, loudly). Proven on GKE Standard;
  on Autopilot, dry-run the rendered Deployment server-side before an app
  relies on it, since Autopilot restricts volume types and does not list this
  one either way. The node pulls the image with its own credentials on every
  pod start, so the registry is a start-time dependency: while it is
  unreachable, a pod that restarts cannot come back.

What you have now is the whole install. Secrets are Kubernetes Secrets you
create in the app's namespace (`secrets_from` names them), and sizing runs
on Managed Prometheus with the sizing agent. The sibling projects that go
further are upgrades with their own prerequisites, not steps you skipped:
[ladon](https://github.com/tenishai/ladon) puts secrets on a vault you
unseal and front with an identity proxy, and
[noto](https://github.com/tenishai/noto) adds dashboards, logs and traces
on a Standard cluster of its own. Reach for them when you have several
teams, secrets that must rotate, or an estate to watch; a first install
needs neither.

---

## Existing cluster

In `gojira.env` leave `GOJIRA_TERRAFORM` empty, set `CLUSTER=<your cluster>`
and `GOJIRA_PREPARE_PROJECT=1`; `make install` then grants Gojira's identity into
your project (APIs, service account, roles, image repo, Workload Identity)
before installing. For a cluster with no public endpoint also set
`GOJIRA_DNS_ENDPOINT=1`. Steps 3 onward are unchanged.

### Keeping the control plane closed to the internet

By default Gojira and the CI it generates reach a cluster through GKE's
DNS-based endpoint: IAM-authenticated, audit-logged, reachable from
GitHub-hosted runners and from a laptop off the VPN. It needs
`allowExternalTraffic` on the cluster, which Gojira turns on at registration
and re-asserts on every provision. Authorized networks do not apply to it;
a Google credential with `container.clusters.get` and the cluster's RBAC is
the whole gate.

If your policy wants the control plane reachable only from inside the
network, register the cluster with access **internal** instead (the
clusters page, or `PATCH /api/v1/clusters/<name>` with `{"access":
"internal"}` for one already registered). Then:

- Gojira reaches it by the private endpoint (`get-credentials
  --internal-ip`), so Gojira must run inside that network, which it does
  when installed on the cluster itself.
- The generated workflows do the same, so `GOJIRA_CI_RUNNER` must name a
  self-hosted runner inside that network (actions-runner-controller on the
  cluster, a VM in an authorized range). Build-from-repo apps need docker
  on that runner.
- The `ensure_cluster_dns_access` step leaves the DNS endpoint as you set
  it, and the install scripts use the private endpoint with
  `GOJIRA_CLUSTER_ACCESS=internal` in `gojira.env`, run from a machine on
  the VPN.

Order for a cluster already running with DNS access: set the runner
variable and the access mode, press **Provision** again on each app so
its workflows regenerate, watch one deploy land through the new runner,
then close the DNS endpoint on the cluster (`allowExternalTraffic:
false`). Closing it first breaks every deploy until the workflows are
regenerated.

---

## Removing it

```bash
make uninstall                       # shows the plan, asks for the project id
make uninstall ARGS=--check          # changes nothing; lists what Gojira has here
make uninstall ARGS="--purge --yes"  # everything, no question
```

Run it from the clone that ran `make install`: the terraform state in its
`terraform/` is what takes the cluster down. From any other clone step 4
is skipped with a sentence saying where to go, and the sweep lists the
cluster as left. A rerun after a partial purge is safe: what the first run
already removed is passed over, not failed on.

Every run except `--check` writes its full output to `~/.gojira/uninstall-<time>.log` (or `GOJIRA_UNINSTALL_LOG`) and names the file at the end: the cluster's own logs go in step 3, so when an app's teardown fails partway this is the record of why. Under `--purge`, a deploy identity such a failure left behind is removed by the sweep rather than listed.

`terraform destroy` on its own is not an uninstall. Terraform made the
cluster; the install script and Gojira made the rest, and Gojira's record of
what it created lives in the cluster. So the order is fixed, and the script
keeps it:

1. every connected app, through Gojira: namespaces, deploy service account,
   role grants, images
2. the front door, if you set a hostname (the ingress first, which releases
   the load balancer)
3. Gojira's namespace, which releases its data disk (GKE keeps disks when a
   cluster is deleted)
4. the platform: `terraform destroy` when `terraform/` holds the state. On an
   [existing cluster](#existing-cluster) it removes Gojira's service account,
   roles and image repo, and leaves your cluster alone.
5. a sweep of the project. Anything of Gojira's still there is listed and
   the command fails.

Last run against a real project (2026-09-29): six minutes, and the project
held exactly what it held before Gojira.

| Kept unless `--purge` | Why |
|---|---|
| Identity pool `gojira` | Every app shares it. It costs nothing and grants nothing once the apps are gone. |
| Image repo `apps` | `--purge` deletes the images of the apps it removed and then the repo; it keeps the repo only for images Gojira did not build, and names them. |
| Bucket `<project>_cloudbuild` | Cloud Build's, and other builds in the project may use it. |

Never touched: enabled APIs (disabling one can break things Gojira never
knew about), and the files the setup PR added to your repos (`gojira.yaml`,
`k8s/`, `.github/workflows/gojira-ci.yaml`). Those are yours.

**Installing again** into the same project works, `--purge` or not:
`make install`, then steps 3 to 7 unchanged. Google keeps a deleted identity pool's name for 30 days, and
Gojira restores the pool instead of creating it. A repo that still holds
Gojira's files gets a small refresh PR instead of the full setup PR, or no
PR at all when nothing changed. In that case the app reads `active` with
nothing running, because no merge started a deploy: press **Redeploy** on
the app page.

If the project was made for this, `gcloud projects delete <project>` is the
shortest uninstall there is.

---

## When something goes wrong

| You see | Cause | Do |
|---|---|---|
| `make install`: `terraform wants to delete or replace` | A change to `terraform/` or the provider would rebuild something. `make install` never does that on its own. | `terraform -chdir=terraform plan`, read it, apply it yourself if it is what you want |
| `make install`: `terraform.tfvars names another project` | A leftover `terraform/terraform.tfvars` would win over `gojira.env` | Delete it; `gojira.env` is enough |
| `make install`: `PERMISSION_DENIED` from `gcloud.builds.submit`, three times | Not the first-use delay: your account cannot use Cloud Build | Grant yourself Cloud Build Editor, or use an Owner account |
| `kubectl`: `User "<other account>" cannot ...` | The per-cluster token cache, see the note at the top | `rm ~/.kube/gke_gcloud_auth_plugin_cache`, or use a separate `KUBECONFIG` |
| Registration: cluster unreachable | DNS-based access is off on the cluster | `./scripts/preflight.sh --fix` |
| Registration with access internal: cluster unreachable | Gojira is not inside the cluster's network, or the private endpoint's authorized networks exclude it | Run Gojira on the cluster (or a peered network), or register with access dns |
| Connect: `has no Dockerfile and its gojira.yaml names no image:`, or the repo is not listed | Neither a `Dockerfile` nor an `image:` contract on the default branch, or the App is not installed on that repo | Add one, or [github-app-setup.md](github-app-setup.md) §3 |
| `/api/v1/...`: `404 Not Found` on a route this page names | The running image is older than your checkout | `make install` |
| App is `active` but its namespace is empty | Connected a repo that already held Gojira's files; nothing was merged, so nothing deployed | **Redeploy** on the app page |
| Redeploy: `403 Resource not accessible by integration` | The GitHub App has Actions: Read | Set Actions to Read & write on the App, accept the change on the installation ([github-app-setup.md](github-app-setup.md) §1) |
| Provision: `the GitHub App cannot manage Actions variables` | The App lacks the Variables permission; the generated workflows read the install's registry, identity and cluster from repository variables Gojira sets | Set Variables to Read & write on the App, accept the change on the installation, press **Provision** again |
| Provision: `this install does not grant roles/...` | The contract's `identity.gcp_roles` names a role outside `GOJIRA_GRANTABLE_ROLES` | Add it to the allowlist and `make install`, or take it out of the contract — a reviewer's decision either way |
| Provision: `k8s/base/... holds a ClusterRole` | The app's base carries a cluster-scoped object the deploy identity cannot create | Move it out of `k8s/base`; apply the ClusterRole yourself and name it in `cluster_roles:` |
| Provision: `this install does not bind ...` / `ClusterRole ... is not on cluster ...` | The name is not on `GOJIRA_GRANTABLE_CLUSTER_ROLES`, or you have not applied it there | Add it to the allowlist and `make install`; `kubectl apply` the ClusterRole on that cluster |
| Provision: `identity.gsa names ... which does not exist` | The contract adopts a service account that is not there | Create it (or fix the email); Gojira adopts accounts, it never makes the one you name |
| A repo's CI: `GOJIRA_DEV_AR_REPO is not set on this repository` | The workflow is Gojira's but this install never set the repo's variables (a fork, a repo moved between installs, or an install upgraded before its App had the Variables permission) | Connect the repo here, or press **Provision** again on its app |
| App stays `awaiting_pr` after the merge | Gojira has not been told | Step 7: **check PR merge** or the `sync` call |
| Pod `exec format error` | An image built on ARM for amd64 nodes | Build through `make install` (Cloud Build), not local Docker |

`./scripts/preflight.sh` re-checks tools, account, project, cluster access and
the App credentials in a few seconds and is the first thing to run.
