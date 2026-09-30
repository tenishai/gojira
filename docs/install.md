# Installing Gojira

From an empty GCP project to an app Gojira deployed, measured and can
rightsize: about 40 minutes, most of it waiting for a cluster. Steps 1 to 8
were run in this order into a project that had never seen Gojira (last run:
2026-09-29). Where the run surprised us, the page says so.

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
| `gke-gcloud-auth-plugin` | `gcloud components install gke-gcloud-auth-plugin` |
| A GitHub App | Ten minutes: [github-app-setup.md](github-app-setup.md) §1–§3. You need its numeric **App ID** and its **private key** (`.pem`). |

```bash
git clone https://github.com/tenishai/gojira && cd gojira
gcloud auth login
gcloud auth application-default login     # terraform reads these credentials
gcloud config set project <your-project>
```

> **More than one Google account on this machine?** `kubectl` caches its
> token per cluster, not per account. A single `kubectl` call made under the
> wrong account is then reused for an hour by every later call, which fails
> with `User "<other account>" cannot get resource`. Give this install its
> own kubeconfig: `export KUBECONFIG=~/.kube-gojira/config`.

---

## 1. Create the cluster (about 12 minutes)

```bash
cd terraform
cp terraform.tfvars.example terraform.tfvars   # set project_id
terraform init
terraform apply
eval "$(terraform output -raw get_credentials_cmd)"
cd ..
```

This makes an Autopilot cluster named `gojira-sandbox`, an Artifact Registry
repo for Gojira's own image, and Gojira's service account bound to its
in-cluster identity. State stays on your machine (`terraform/` ignores it);
keep it, teardown needs it.

## 2. Install Gojira (about 6 minutes)

```bash
cp gojira.env.example gojira.env
$EDITOR gojira.env        # PROJECT, REGION, GOJIRA_GITHUB_APP_ID, GOJIRA_GITHUB_APP_PEM
make install
```

`make install` checks your tools and the cluster, builds the image in Cloud
Build, creates the secret (your App key, plus a webhook secret and an API
token it mints), applies the manifests and waits for the pod. It is safe to
rerun; rerunning is also how you upgrade and how you change any value in
`gojira.env`.

In a project that has never used Cloud Build, the first build is refused for
about a minute after the API is enabled. The script waits and retries; you
will see `Cloud Build is not ready in this project yet`.

It finishes by printing how to open the UI and how to read the two secrets.

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

> **Gojira may restart once here.** The first app brings alert rules, which
> makes Google's managed Prometheus start a high-priority pod; on a full node
> that pod evicts Gojira. It is back in about 30 seconds with its state
> intact. Re-run the port-forward if it dropped.

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
- **Your own app**: anything with a `Dockerfile` at its root. Port, health
  path and scaling bounds are yours to declare in `gojira.yaml`; a repo that
  already has `k8s/` manifests is adopted, not overwritten.

---

## Existing cluster

Skip step 1. In `gojira.env` set `CLUSTER=<your cluster>` and
`GOJIRA_PREPARE_PROJECT=1`; `make install` then grants Gojira's identity into
your project (APIs, service account, roles, image repo, Workload Identity)
before installing. For a cluster with no public endpoint also set
`GOJIRA_DNS_ENDPOINT=1`. Steps 3 onward are unchanged.

---

## Removing it

Order matters. Gojira's record of what it created lives in the cluster, so
remove the apps while Gojira is still running.

```bash
# 1. each app: its namespace, deploy identity, role grants and images
curl -s -X DELETE localhost:8080/api/v1/apps/1 -H "Authorization: Bearer $TOKEN"

# 2. Gojira's own namespace, which releases its data disk
#    (GKE keeps disks when a cluster is deleted)
kubectl delete namespace gojira

# 3. the cluster, Gojira's image repo and its identity
cd terraform && terraform destroy
```

What this leaves in the project, on purpose or not yet automated:

| Left behind | Why | Remove with |
|---|---|---|
| Workload identity pool `gojira` | Shared by every app. A deleted pool's name stays reserved for 30 days, so deleting it blocks a reinstall for a month. | Leave it, unless the project is done with Gojira |
| Artifact Registry repo `apps` | Holds every app's images | `gcloud artifacts repositories delete apps --location <region>` |
| Cloud Build bucket `<project>_cloudbuild` | Source uploads from `make install` | `gcloud storage rm -r gs://<project>_cloudbuild` |
| Enabled APIs | Disabling an API can break things Gojira never touched | Leave them |
| With a hostname: static IP `gojira-ingress-ip`, the load balancer, the sign-in client | Made by `make install`, outside terraform | Delete the ingress **before** step 3 (`kubectl -n gojira delete ingress --all`), then `gcloud compute addresses delete gojira-ingress-ip --global` |
| The files the setup PR added to your repo | They are yours now | Delete `gojira.yaml`, `k8s/`, `.github/workflows/gojira-ci.yaml` |

If the project was made for this, `gcloud projects delete <project>` removes
all of it at once.

---

## When something goes wrong

| You see | Cause | Do |
|---|---|---|
| `terraform apply`: `Identity Pool does not exist` | The identity binding ran before the cluster existed (fixed 2026-09-29) | Pull, or run `terraform apply` again |
| `make install`: `PERMISSION_DENIED` from `gcloud.builds.submit`, three times | Not the first-use delay: your account cannot use Cloud Build | Grant yourself Cloud Build Editor, or use an Owner account |
| `kubectl`: `User "<other account>" cannot ...` | The per-cluster token cache, see the note at the top | `rm ~/.kube/gke_gcloud_auth_plugin_cache`, or use a separate `KUBECONFIG` |
| Registration: cluster unreachable | DNS-based access is off on the cluster | `./scripts/preflight.sh --fix` |
| Connect: `repo has no Dockerfile at its root`, or the repo is not listed | No `Dockerfile` on the default branch, or the App is not installed on that repo | Add one, or [github-app-setup.md](github-app-setup.md) §3 |
| `/api/v1/...`: `404 Not Found` on a route this page names | The running image is older than your checkout | `make install` |
| App stays `awaiting_pr` after the merge | Gojira has not been told | Step 7: **check PR merge** or the `sync` call |
| Pod `exec format error` | An image built on ARM for amd64 nodes | Build through `make install` (Cloud Build), not local Docker |

`./scripts/preflight.sh` re-checks tools, account, project, cluster access and
the App credentials in a few seconds and is the first thing to run.
