# Dogfooding Gojira

End-to-end: stand up a throwaway GKE sandbox, run Gojira **in-cluster**, and use
it to deploy [`gojira-demo`](https://github.com/shaz/gojira-demo) — then patch
the running app and watch Gojira open a drift-capture PR.

This is the production-shaped path (in-cluster Gojira, GitHub App auth). It
skips the public webhook on purpose — GitHub reaching a public HTTPS endpoint is
the fiddly part — so PR-merge and drift are driven by the in-UI **check PR
merge** and **Scan now** buttons instead.

---

## Prerequisites

- A **sandbox GCP project** with billing (throwaway — Gojira gets broad IAM in it).
- `gcloud` authed as a principal who can apply the Terraform (project IAM admin).
- `terraform`, `kubectl`, `openssl` locally (no docker: images build in Cloud
  Build). `./scripts/preflight.sh` checks the lot.
- A **GitHub App** (see [github-app-setup.md](github-app-setup.md)) installed on
  the repo you'll deploy. You need its **App ID** and a **private key** (`.pem`).
  The webhook secret and the API token are minted by bootstrap.

```bash
export PROJECT=your-sandbox-project
export REGION=us-central1
```

---

## 1 — Stand up the sandbox

```bash
cd terraform
cp terraform.tfvars.example terraform.tfvars   # set project_id
terraform init
terraform apply
```

Note the outputs — you'll reuse them:

```bash
terraform output                       # gojira_gsa_email, ar_repo_path, get_credentials_cmd, ...
eval "$(terraform output -raw get_credentials_cmd)"   # kubeconfig for the sandbox
cd ..
```

## 2 — Bootstrap Gojira into the cluster (one command)

Everything between terraform and the UI is scripted — image build (via Cloud
Build, so ARM laptops like Apple Silicon / DGX Spark are safe from the
amd64 `exec format error` trap), namespace, GitHub App secret, manifests
rendered into a temp copy (your tree stays clean), and the rollout wait:

```bash
cp gojira.env.example gojira.env      # PROJECT, GOJIRA_GITHUB_APP_ID, GOJIRA_GITHUB_APP_PEM
make install                          # = ./scripts/install.sh → preflight → bootstrap
```

Idempotent — rerun it freely (it preserves the minted webhook secret and API
token and re-applies everything else; it prints how to read both at the end). Prefer the manual steps? They live in the
script, in order, with comments.

## 3 — Open the UI

```bash
kubectl -n gojira port-forward svc/gojira 8080:80
```

Browse to <http://localhost:8080>.

---

## 4 — Register the sandbox cluster

In the UI → **Clusters** → add:

| Field | Value |
|---|---|
| name | `sandbox` |
| gcp_project | `$PROJECT` |
| location | `$REGION` |
| gke_cluster | `gojira-sandbox` |

Registration probes connectivity (`get-credentials` over the DNS endpoint, enabling it on the cluster first if off); a
green result means Gojira (via Workload Identity) can reach the cluster.

## 5 — Connect gojira-demo

UI → **Connect** → pick `shaz/gojira-demo`, then:

- **class:** `web`
- **environment(s):** one `dev` env on the `sandbox` cluster, namespace `demo`

Gojira provisions GCP-side identity (deploy GSA, WIF pool + per-repo binding,
Artifact Registry, namespace) and opens a **setup PR** on `gojira-demo`.

## 6 — Merge the setup PR → deploy

1. Merge the setup PR on GitHub (it adds `.github/workflows`, `k8s/`, `gojira.yaml`).
2. Back in the UI, click **check PR merge** on the app page — Gojira flips the
   app to `active`.
3. The merge triggers `gojira-ci` in the demo repo (WIF auth, no keys): it builds,
   pushes to Artifact Registry, and `kubectl apply`s to the sandbox. Watch the
   Actions run, then:

```bash
kubectl -n demo get deploy,pods
```

You now have an app Gojira deployed, with no hand-written YAML.

---

## 7 — Exercise drift (the Phase 2 payoff)

Simulate an operator hot-patching production:

```bash
# Bump both — a request above the class's 500m limit alone would be rejected
# by the API server (requests must be <= limits).
kubectl -n <namespace> set resources deploy/gojira-demo --requests=cpu=750m --limits=cpu=1500m
```

In the UI, on the app page → **Drift** → **Scan now**. Within the scan you'll see:

- a **Drift** finding: `containers.app.resources.requests.cpu` — git `250m` → cluster `750m`
- a **capture PR** link on `gojira-demo` that writes the live value into the env
  overlay, so merging it makes git match the cluster.

The cluster's change is **captured, not reverted** — exactly the Phase 2 DoD.
(Leave `drift.enabled: true` in the ConfigMap and the central scheduler does the
same sweep every 15 min without the button.)

---

## Teardown

```bash
cd terraform && terraform destroy
```

This deletes the cluster, AR repo, and Gojira's GSA. Per-app GSAs/WIF that
Gojira created in the project are not Terraform-managed — remove them with
`gojira destroy` (when it lands) or by hand if you're keeping the project.
