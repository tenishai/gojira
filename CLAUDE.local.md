# RESUME HERE (written 2026-10-01 ~21:30Z)

LITELLM IS LIVE on the dogfood: tenishai/litellm (repo created by Shaz,
pushed), Gojira app id 2, setup PR #1 merged by Shaz, CI on spark, pod 1/1
on arm64 Scale-Out node, /v1/models = gemini-2.5-flash + gemini-2.5-pro,
a real completion through Vertex via WI (GSA litellm@shaz-learning,
aiplatform.user), app status ACTIVE. Secret litellm-secrets created by
Shaz; the key is in ~/litellm-master-key (reuse for cabal-secrets).
Classifier blocks for me: new-repo creation, PR merges ("merge without
review"), Secret writes → all three are Shaz's hands.
gojira-demo ACTIVE (set_ci_runner exists). cabal#240 OPEN (Rich, 0
reviews). kobo#81/#82 OPEN (Kyle).
READY IN SCRATCH: scratchpad/stack/dogfood/cabal-config.yaml (identity.toml
ConfigMap for ns cabal; validated with cabal's load_identity in
scratchpad/stack/cabal/venv — techne 0.18.0 from the tenish index).
NEXT: (1) cabal#240 merged → connect tenishai/cabal via API (name cabal,
class gateway, dev/gojira-sandbox/ns cabal) → kubectl apply -f scratchpad/
stack/dogfood/cabal-config.yaml → Shaz: `kubectl -n cabal create secret
generic cabal-secrets --from-literal=LITELLM_MASTER_KEY=$(cat ~/litellm-
master-key) --from-literal=CABAL_MASTER_KEY=$(python3 -c "from
cryptography.fernet import Fernet;print(Fernet.generate_key().decode())")`
(cryptography is in the cabal venv: scratchpad/stack/cabal/venv/bin/
python) → Shaz merges setup PR → CI → pod → logs grep bootstrap_code →
cbl login via port-forward 7400 → cbl chat archivist. (2) kobo after Kyle
(02:40Z checkpoint). (3) liminal after cabal. (4) gcp_roles contract
design — shape proposed to Shaz (provisioner steps mirroring the CI deploy
identity, KSA via WI, annotation on the rendered SA, allowlist
iam.grantable_roles in config); awaiting his yes before designing.
Standing: no signature in chat; sign PRs/issues as Ember (SRE AI); never
switch the global gcloud account; inventory at ~/Documents/tenishai-stack-
install-inventory-2026-09-30.md; scratch clones at scratchpad/stack/*.

# CHECKPOINT 2026-10-01 (~03:00Z) — gojira#81 merged+deployed; Variables permission gap (PR #82); cabal unblocked

Rich MERGED liminal#117 + cabal#239; Shaz merged gojira#81 → dogfood
`make install` run 7 (configmap GOJIRA_CI_RUNNER=spark). Re-provision of
gojira-demo: set_ci_runner FAILED 403 — Actions variables need the App
permission "Variables" (separate from Actions). Shaz to add Variables RW
on tenish-enterprise + accept on installation; gojira PR #82 = docs row +
step error naming the permission (452 tests). After acceptance: provision
again → set_ci_runner should skip (CI_RUNNER=spark already set by hand).
gojira-demo app currently FAILED status until then.
NEXT: cabal (step 2): contract PR on Rich's cabal (gateway, config_mounts
identity.toml ConfigMap, secrets_from cabal-secrets, data_mount /data,
build_args EXTRA_INDEX_URL=https://us-central1-python.pkg.dev/shaz-
learning/tenish/simple/, arch arm64 + Scale-Out, port 7400, health
/healthz) + LiteLLM as its own tiny Gojira app (repo tenishai/litellm-
proxy: FROM ghcr.io/berriai/litellm, config vertex_ai/gemini-2.5-flash,
WI → a GSA with aiplatform.user); liminal after (needs cabal JWKS +
minted token). kobo waits on Kyle merging kobo#81/#82.

# CHECKPOINT 2026-10-01 (~02:40Z) — KOBO PRs OPEN (#81 model group, #82 contract); RUNNER ISOLATION FIX

Models: Shaz couldn't find the Claude quota page → dogfood uses GEMINI
(org.yaml: vertex_location us-central1, models gemini-2.5-flash + pro;
both verified 200). kobo PR #81 (access/model-group-optional): model_group
empty (new default) → grant/revoke roles/aiplatform.user on vertex_project
directly; conftest pins group for Kai's tests; 4037 pass; ruff 42
pre-existing. kobo PR #82 (deploy/gojira-contract): gojira.yaml (gateway,
/api/healthz, KOBO_CONFIG=/config/config.yaml, secret_mounts kobo-config
+ kobo-github, data_mount kobo-data /data fs_group 1001, arch arm64 +
Scale-Out, service_account kobo) + k8s/base {serviceaccount,pvc,rbac
ClusterRole kobo-room}+kustomization + overlays/dev patch WI annotation
kobo-room@shaz-learning. kustomize builds. Fresh path reads an existing
gojira.yaml's needs (steps.py:421) and preserves base resources.
INCIDENT: generated CI's auth step on spark-tenishai ran `gcloud auth
login --cred-file` into MY ~/.config/gcloud → active account became
gojira-demo-deploy@ (expired WIF token); found via kubectl kustomize
error. FIXED: account restored to schaudhri@fubo.tv, WIF account revoked;
all 3 runner units now Environment=CLOUDSDK_CONFIG=~/actions-runner-<r>/
.gcloud + KUBECONFIG=~/actions-runner-<r>/.kube/config; documented in
gojira #81 (extra commit) + observations.
NEXT (gated on Kyle merging kobo#81+#82): connect tenishai/kobo via Gojira
(name kobo, ns kobo, class gateway) → setup PR → create Secrets in ns kobo
by hand: kobo-config/config.yaml (github_org tenishai, org_repo tenishai/
kobo-config-dogfood, gojira_url http://gojira.gojira.svc.cluster.local,
gojira_token = gojira-github-app/api_token, gojira_cluster gojira-
sandbox, gcp_project shaz-learning, db_url sqlite:////data/kobo.db,
workdir /data/work, ui_dist /srv/ui, github_app_id 5141957,
github_app_installation_id 166677836, github_app_private_key_path
/secrets/github/private-key.pem, iap_audience "") and kobo-github/
private-key.pem from ~/tenish-room.pem → merge setup PR → CI on spark
(arm64) → kobo pod → port-forward → bootstrap (personas/skills repo) →
create an agent → acceptance. OPEN: gojira#81; Rich's liminal#117
cabal#239; Kyle's kobo#81 #82.

# CHECKPOINT 2026-10-01 (~01:45Z) — KOBO GROUNDWORK DONE; next = kobo PRs + connect

Vertex ENABLED on shaz-learning; Gemini 2.5 flash answers (200); Claude
Sonnet 4.6 (Shaz accepted 4.6 in Model Garden; 4.5 not offered) → 429
quota per_base_model = ZERO quota on a fresh project → Shaz to request in
console (IAM&Admin → Quotas, "claude-sonnet-4-6", us-east5; not servable
in us-central1). CREATED: tenishai/kobo-config-dogfood (org.yaml rendered
by kobo's own render_org_yaml: github_org tenishai, skills_repo
kobo-skills-dogfood, buckets shaz-learning-kobo-{skills,personas}, cluster
gojira-sandbox, runtime_image …/runtime/daedalus-runtime:aacb12d, vertex
shaz-learning/us-east5, model_group '', models vertex_ai/claude-sonnet-
4-6, operators []); buckets created (us-central1); GSA kobo-room@shaz-
learning with aiplatform.user, artifactregistry.reader, container.
developer, iam.serviceAccountAdmin, resourcemanager.projectIamAdmin,
storage.admin; WI binding kobo/kobo → kobo-room. tenishai/kobo-config =
FUBO's (don't touch). Kobo bootstrap hard-codes <org>/kobo-config (steps
skip) → finding. kobo image BUILDS on arm64 locally (dpkg arch-aware).
kobo venv at scratchpad/stack/kobo/api/venv.
NEXT: (1) kobo PR: model_group optional → when empty grant roles/
aiplatform.user on vertex_project directly (AccessStep add + destroy
remove; CloudClient/Gcloud impl + fakes + tests); (2) connect tenishai/
kobo via Gojira (name kobo, ns kobo, class gateway?, arch arm64) → setup PR
on Kyle's repo; then PR adding needs to gojira.yaml (config via Secret
kobo-config mounted /config/config.yaml w/ KOBO_CONFIG env, Secret
kobo-github pem mount, data_mount PVC kobo-data /data (sqlite), health
/api/healthz, service_account kobo) + k8s/base/serviceaccount.yaml +
pvc.yaml + ClusterRole for agent namespaces + overlay patch WI annotation;
hand-made Secrets in ns kobo (config.yaml incl gojira_token from
gojira-github-app/api_token; pem); (3) run kobo bootstrap (buckets exist;
personas seed, skills repo); (4) acceptance: create agent → arm64.
OPEN: gojira#81; Rich's liminal#117 cabal#239.

# CHECKPOINT 2026-10-01 (~01:00Z) — demo 2/2 on ARM64; daedalus image built; KOBO PLAN awaiting two decisions

gojira-demo: 2/2 Running on t2a-standard-1 arm64 nodes, /health 200,
image built on spark-tenishai (SSD quota now 2000). daedalus#128 MERGED by
Shaz (daedalus is OURS) → runtime image built on spark (arm64, from the
tenish index) → us-central1-docker.pkg.dev/shaz-learning/runtime/
daedalus-runtime:aacb12d (+latest); `daedalus version` 0.4.0 in it.
KOBO FACTS: tenishai/kobo-config org.yaml = FUBO values (fubo dev
project, fubo vertex, fubo.tv group, fubo WIF) → need a dogfood org
config repo (kobo bootstrap creates from template); AccessStep hard-wires
cloud.add_to_group(model_group) → fails on personal GCP (no Cloud
Identity) → kobo PR to Kyle (optional group); aiplatform NOT enabled on
shaz-learning. Kobo's Dockerfile builds standalone (no sibling contexts)
→ kobo can precede cabal/liminal.
PLAN (proposed): Shaz enables Vertex + Claude (Model Garden) + Gemini on
shaz-learning [DECISION 1]; PRs into Kyle's kobo [DECISION 2]: (a)
gojira.yaml (gateway, data_mount sqlite, config_mounts config.yaml,
secret_mounts App pem, service_account, arch arm64, build_args none) +
k8s/base/serviceaccount.yaml (WI annotation) + rbac for agent namespaces;
(b) model_group optional; (c) employer leaks. Me: GSA kobo-room with
IAM-admin roles (finding: contract lacks app IAM grants vocabulary);
bootstrap org repo + buckets; connect via Gojira; acceptance = kobo
creates an agent → Gojira deploys → answers on A2A on arm64.
OPEN: gojira#81 (CI_RUNNER knob) green, awaits Shaz; Rich's liminal#117,
cabal#239. ~/gojira-dogfood.env has GOJIRA_CI_RUNNER=spark (effective
after #81 + make install).

# CHECKPOINT 2026-10-01 (~00:20Z) — ARM64 END-TO-END PROVEN on the dogfood; PR #81; waiting on SSD quota

Shaz merged #77-#80. Dogfood Gojira rebuilt on main. gojira-demo contract
now `arch: arm64` + node_selector Scale-Out (gojira-demo#14 merged, #13
closed, regenerated setup PR #15 merged). FINDINGS: (1) org variable
CI_RUNNER invisible at run time to private repos on GitHub FREE (listed by
API, empty in vars context) → repo variable set by hand → job ran on
spark-tenishai, built+pushed ARM64 image; (2) runner lacked kustomize →
`gcloud components install kustomize` (on runner PATH via ~/google-cloud-
sdk/bin); (3) deploy step: pod 1 RUNNING on t2a-standard-1 arm64 node,
pod 2 Pending: GCE "QUOTA_EXCEEDED" = SSD_TOTAL_GB per region 500 (4
Autopilot nodes × 100GB boot disk) — API quota request rate-limited →
Shaz raises in console (IAM & Admin → Quotas, SSD_TOTAL_GB us-central1 →
2000). PR #81 (feat/ci-runner-knob): GOJIRA_CI_RUNNER knob + SetCiRunner
step (repo variable via both backends) + install.md runner requirements;
451 tests. ~/gojira-dogfood.env now has GOJIRA_CI_RUNNER=spark (takes
effect after #81 merges + make install).
NEXT: quota → demo 2/2 on arm64 → cabal (step 2: build_args
EXTRA_INDEX_URL=https://us-central1-python.pkg.dev/shaz-learning/tenish/
simple/, config_mounts identity.toml, gateway class, arch arm64; LiteLLM
as own app) → liminal → daedalus image → kobo. Rich's PRs still open:
liminal#117 cabal#239 daedalus#128.

# CHECKPOINT 2026-09-30 (~23:30Z) — ORG RUNNER LIVE; arch/CI_RUNNER PR #80; waiting on merges

Shaz: "everyone uses local runners" → no billing fix. DONE: org-level
runner spark-tenishai (~/actions-runner-tenishai, user unit gh-runner-
tenishai.service, labels spark,arm64,linux, Default group visible to all
repos); org variable CI_RUNNER=spark (visibility ALL). Needed `gh auth
refresh -s admin:org` (Shaz did it; token now has admin:org).
FACTS PROVEN: spark buildx = linux/arm64 ONLY (no binfmt); Autopilot
us-central1 gives an arm64 node in ~100s with nodeSelector
{kubernetes.io/arch: arm64, cloud.google.com/compute-class: Scale-Out} +
toleration kubernetes.io/arch=arm64:NoSchedule; plain arch selector is
REFUSED by GKE Warden. → dogfood = arm64 end to end.
GOJIRA PRs OPEN, ALL GREEN: #77 (uninstall --check wording), #78
(config_mounts/secret_mounts/build_args), #79 (WIF provider owner
check/widen), #80 (arch + node_selector in contract; generated CI runs-on
${{ vars.CI_RUNNER || 'ubuntu-latest' }}; --platform linux/<arch>;
goldens 2 lines each; 447 tests). Rich's: liminal#117 cabal#239
daedalus#128.
NEXT after Shaz merges: make install (dogfood, GOJIRA_ENV=~/gojira-
dogfood.env) → gojira-demo contract: arch: arm64 + node_selector
{cloud.google.com/compute-class: Scale-Out} (close setup PR #13, provision
again → new setup PR → merge → CI on spark → pod on arm64 node) → then
cabal (step 2) with build_args EXTRA_INDEX_URL + config_mounts identity.
toml + gateway class; LiteLLM as its own app; liminal; daedalus image;
kobo. Findings to fix later: bootstrap restarts only on digest change
(not on secret change); gojira-demo#13 stale (superseded by regeneration).

# CHECKPOINT 2026-09-30 (~23:00Z) — ORG APP LIVE; repos moved to tenishai; BLOCKED on tenishai Actions billing

ORG APP: tenish-enterprise, id 5141957, installation 166677836, owner
tenishai, ALL repos, perms actions/administration/contents/pull_requests/
workflows write + metadata read (verified via JWT → /app). pem at
/home/shaz/tenish-room.pem (600). ~/gojira-dogfood.env now points at it;
make install run 5 applied the secret (app_id 5141957) + rolled.
REPOS TRANSFERRED: shaz/hello-app → tenishai/hello-app, shaz/gojira-demo
→ tenishai/gojira-demo. Cluster registered (gojira-sandbox); app 1 =
tenishai/gojira-demo AWAITING_PR (setup PR tenishai/gojira-demo#13 = the
#76 unescape only). Redeploy via Gojira route DISPATCHED OK (Actions
write works) but the run FAILED IN 3s: "job was not started because
recent account payments have failed or your spending limit needs to be
increased" = tenishai org Actions billing (the 9/24 wall). Generated CI
uses ubuntu-latest → every org tenant app is dead until billing is fixed
(recommended, Shaz/Rich) or generated CI learns CI_RUNNER + org runner on
spark (arm64 vs amd64 nodes → arch question / ARM pool).
WIF: provider condition was repository_owner=='shaz' → updated by hand to
'tenishai'; PR #79 makes EnsureWifProvider read/widen the condition (kept
w/ warning if hand-written). OPEN gojira PRs: #77 (uninstall --check),
#78 (config_mounts/secret_mounts/build_args), #79. Rich's: liminal#117,
cabal#239, daedalus#128.
NEXT once CI can run: merge gojira-demo#13 → sync → deploy → then cabal
(step 2). Finding to fix later: bootstrap only restarts on digest change,
not when the App secret changed (restarted by hand this time).

# CHECKPOINT 2026-09-30 (~21:30Z) — STACK AUDIT DONE (7 repos); STEP 1 PRs OPEN; dogfood back up

Shaz: "let's do the PRs" (straight into Rich's/Kyle's repos) + kobo's
GitHub App = tenishai ORG app (Shaz is org admin, verified; no org apps
installed yet). I recommended ONE org App shared by Gojira+kobo,
installed on All repositories (name e.g. tenish-room; webhook inactive;
perms Contents/PRs/Workflows/Actions/Administration RW + Metadata R;
events PR + workflow_run). Shaz creates it in the browser; then App id,
pem → spark, installation id → me; switch dogfood Gojira to it; move
hello-app + gojira-demo into the org.
INVENTORY: ~/Documents/tenishai-stack-install-inventory-2026-09-30.md
(stage 1: techne/liminal/cabal/daedalus/kobo; stage 2: argus/ladon).
Root gap: images need private sibling build contexts; nothing publishes
images. Other: SQLite+PVC everywhere (gateway class), health endpoints
never fail, config-as-file (Gojira lacks file-mount vocabulary), open
auth defaults, kobo employer leaks. argus wants a STANDARD cluster (ties
to the ARM-pool item); ladon already ships a gojira.yaml.
STEP 1 DONE: AR python index us-central1-python.pkg.dev/shaz-learning/
tenish (repo `tenish`), techne 0.18.0 + liminal 0.2.0 published + clean
pip install proven. Dockerfile pattern: `FROM scratch AS techne` empty
stage overridable by --build-context; else --build-arg EXTRA_INDEX_URL
(+AR_TOKEN → oauth2accesstoken creds); NOT named PIP_EXTRA_INDEX_URL (pip
reads it; ARG leaks into later RUNs → password prompt). Built+verified
locally on spark (arm64 fine for pure-python). PRs: liminal#117,
cabal#239, daedalus#128 (daedalus also fixes Makefile missing liminal
context). FINDING: PyPI names `techne` (0.0.2) and `cabal` (0.3.0) are
TAKEN; liminal/daedalus free — told Rich in the PRs.
Dogfood: make install run 4 → Gojira 1/1 (20:33Z); cluster NOT registered,
nothing connected yet (waiting for the org App to do it in tenishai).
NEXT: (2) cabal-core + LiteLLM as Gojira apps (needs Gojira: build_args
in contract to pass EXTRA_INDEX_URL; file-mount for identity.toml);
(3) liminal; (4) daedalus runtime image via Cloud Build into shaz-
learning AR; (5) kobo. Scratch clones at scratchpad/stack/* (branches
pushed). PR #77 (uninstall --check wording) still open on gojira.

# CHECKPOINT 2026-09-30 (~06:30Z) — DECISION: approach A (kobo #73 as built); Prudence connects as pr-reviewer; rename work DROPPED

Options laid out for Shaz: A = repo decides the name (kobo #73 as merged;
fork per instance, e.g. "mr-strict"), B = Gojira renames base + all
overlays (Kyle's revised ruling), C = instance-as-overlay + API states
names. Shaz chose A: agent repos are thin over the SDK, so forks are
cheap; convention "Deployment named after the agent" enforced by kobo at
connect. Prudence: connect as `pr-reviewer` (no rename, no prod cutover;
"Prudence" is the Slack name). Shaz talks to Kyle in the morning; message:
"#73 as built is what we want; drop the Gojira-renames ask; Prudence
connects as pr-reviewer". C stays a future option if forks hurt.
Segment-proxy check: overrides.hpa {min 20,max 150,memory 80} rendered →
minReplicas 20 / maxReplicas 150 / memory target 80, nothing clamps; gap =
bounds are ONE block per contract (US=EU) → per-environment overrides =
the "parallel regional envs" item (small, independent).
Shaz will DOGFOOD UNINSTALL himself next (commands given: on spark, cd
~/src/git/gojira, CLOUDSDK_CORE_ACCOUNT + KUBECONFIG=~/.kube-dogfood/config
+ GOJIRA_ENV=~/gojira-dogfood.env; make uninstall ARGS=--check → make
uninstall → --check). Dogfood currently: gojira-demo ACTIVE.
QUEUE after that: per-env HPA overrides (segment-proxy); ARM pool on
dogfood; stack install audit (kobo/liminal/cabal → issues to Kyle/Rich;
deploy kobo THROUGH Gojira as the test); Prudence: delete app 17, close
infra-agent-pr-reviewer#35, connect as pr-reviewer, hand-add config_from
[pr-reviewer-env] (#61 still open), retire ArgoCD pr-reviewer-dev.
#62 (Rich's arch cards) awaits his fix. Issues open: #61 #72 #73 #75.

# CHECKPOINT 2026-09-30 (~05:40Z) — ALL MERGED; DEV REBUILT; #62 REVIEWED; ADOPT-RENAME DESIGN PROPOSED

Shaz merged #70 #71 #74 #76 (+#66). Feedback saved: NO signature in chat
(memory feedback_no_signature_in_chat); sign only outward. Fubo dev Gojira
REBUILT from main fbf01f8 (make install, fubo account): pod 1/1 prio 1000,
digest 94af016d…, /healthz 200, front door re-asserted; default kubeconfig
current-context now = fubo dev. Dogfood: gojira-demo ACTIVE at 50m/64Mi,
Redeploy WORKS (Shaz fixed App Actions perm). #62 = Rich's arch cards
(rebased on main; its own checker FAILS 3 anchors; reviewed CHANGES_
REQUESTED: bump anchors for #60/#66, adopt line, line-number refs, wire
checker into CI). Local branches pruned; only main left (+2 old ones).
NEXT = ADOPT-RENAME (design proposed to Shaz, awaiting approval): rename
map from old base (kind,name)→rendered names; retarget file+inline patches
and rewrite patch-file metadata.name; namespace → env ns for managed
overlays; images[].name→gojira-app; ALL overlays (Kyle) with a PR-body
report + out-of-repo checklist (secrets in old ns, ArgoCD app, KSA
binding). Fixture = Prudence repo shape. Then Prudence: delete app 17,
close infra-agent-pr-reviewer#35, connect as `prudence`. Then Kai flips
kobo mismatch→warning.

# CHECKPOINT 2026-09-30 (~01:30Z) — ONE COMMAND EACH WAY, PROVEN; PR #74 (stacked on #70 → #71)

Shaz approved make install running terraform; he closed shaz/hello-app#2.
BUILT (branch feat/install-runs-terraform, PR #74): GOJIRA_TERRAFORM=1 in
gojira.env → install.sh runs scripts/tf.sh tf_apply before bootstrap
(TF_VAR_* from gojira.env, token from gcloud, refuses tfvars naming another
project, REFUSES any plan with a delete/replace; refuses with
GOJIRA_PREPARE_PROJECT=1). uninstall.sh now uses tf.sh (tf_owns_project,
tf_destroy). tests/test_install.py (7). 432 tests.
LIVE: uninstall --purge 6 min → make install 16 min (17 resources, Gojira
1/1) → register + connect hello-app → ACTIVE BUT EMPTY (no diff, no PR, no
deploy; issue #75) → UI Redeploy = 403 (dogfood App 4231856 has Actions:
Read; docs/github-app-setup.md said Read → fixed to Read & write) → I
dispatched CI with `gh workflow run gojira-ci.yaml -R shaz/hello-app` →
2/2, /health 200. Gojira not evicted (2nd run in a row at priority 1000).
~/gojira-dogfood.env now has GOJIRA_TERRAFORM=1.
SHAZ TO DO: merge #70 → #71 → #74 in that order (#66 still open); on the
dogfood GitHub App (gojira-sandbox, 4231856) set Actions: Read & write and
accept on the installation, else Redeploy/rollback 403.
ISSUES OPEN (mine): #72 front door IAP API, #73 drift vs Autopilot, #75
active-but-empty after reconnect.
LESSONS: `pkill -f <pattern>` and `pgrep -f <pattern>` inside a bash -c
match their OWN command line (killed my shell / hung a loop) — never use
them with a pattern that appears in the command itself. Long installs: run
with run_in_background, not a 600s foreground.
NEXT: after merges rebuild Fubo dev Gojira from main; adopt-rename design
(Kyle's ruling); ARM pool on dogfood; multi-agent-per-repo; regional envs;
periodic stranger test in a FRESH project (not yet set up).

# CHECKPOINT 2026-09-29 (night) — UNINSTALL BUILT + PROVEN: full remove→reinstall cycle on shaz-learning; PR #71

Shaz: "let's do the destroy now"; modest priority OK; port-forward instead
of front door for the dogfood = fine (answered: not a hack; can't take
webhooks or per-person sign-in; front door stays separate → issue #72).
BUILT (branch feat/uninstall, PR #71, STACKED on #70 → Shaz merges #70
first): scripts/uninstall.sh + `make uninstall ARGS="--purge --yes|--check"`
(order: apps via API → front door → ns gojira → platform [terraform destroy
if terraform/ state names PROJECT; reverse prepare-project if
GOJIRA_PREPARE_PROJECT=1] → purge → sweep); GET /api/v1/apps + /api/v1/
clusters; PriorityClass gojira-platform (1000, preemptionPolicy Never);
gcp.py: WIF pool/provider DELETED → undelete; service_account_exists via
LIST (describe on a recently deleted SA = PERMISSION_DENIED even to owner —
the July cinder delete/re-add wall). 425 tests.
LIVE PROOF: uninstall --purge 6 min exit 0; independent inventory clean
(pre-existing shaz-testing-1/appspot/larql-new only; pool gojira DELETED);
terraform apply 17 in one pass 8.5 min; make install 7.5 min; connect →
pool restored, SA recreated, refresh PR shaz/hello-app#3 merged by me →
CI success → 2/2, /health 200, ACTIVE; Gojira not evicted this time.
DOGFOOD STATE NOW: installed from feat/uninstall @ 608fda9, hello-app
active. shaz/hello-app#2 (old drift capture PR) still OPEN/stale.
ISSUES FILED: #72 front door IAP OAuth API shutdown (contradiction w/ 7/26
note); #73 drift scan reports Autopilot's own adjustments + raw units.
NOT RUN LIVE: front-door removal; existing-cluster (prepared) path.
FLAKY: tests/test_web.py::test_reprovision_of_auto_merge_app_merges_a_
fresh_pr failed 1 of 5 full runs (timing; not mine).
OPEN FOR SHAZ: merge #70 then #71; #66 still open. IDEAS NOT BUILT: make
install running terraform itself (symmetry with uninstall); fresh-project-
per-run for the periodic stranger test; uninstall cleaning other registered
clusters. NEXT: rebuild Fubo dev Gojira from main after merges; adopt-
rename design (Kyle's ruling); ARM pool; multi-agent-per-repo; regional envs.
DOGFOOD COMMANDS: export CLOUDSDK_CORE_ACCOUNT=shahzadchaudhri@gmail.com
KUBECONFIG=~/.kube-dogfood/config GOJIRA_ENV=~/gojira-dogfood.env; terraform
needs GOOGLE_OAUTH_ACCESS_TOKEN=$(gcloud auth print-access-token).

# CHECKPOINT 2026-09-29 (evening, post-compact) — DOGFOOD ACCEPTANCE DONE; PR #70; destroy = open question

#68 #69 MERGED (Shaz). hello-app#1 merged by me (acceptance test) → CI green
1m48s → ns hello 2/2, /health 200, digest-pinned → /sync → ACTIVE. Dogfood
image predated #67 (/sync 404) → `make install` from main rebuilt + rolled
(roll step correct). Drift exercise run: capture PR shaz/hello-app#2 OPEN
(cluster now 750m/1500m cpu, mem 768Mi by Autopilot) — left as is.
DOGFOOD KUBECONFIG: ~/.kube-dogfood/config (KUBECONFIG=… + CLOUDSDK_CORE_
ACCOUNT=shahzadchaudhri@gmail.com). WHY: gke-gcloud-auth-plugin cache is
per-CONTEXT not per-account; a call w/o the env var at 21:34Z cached a
fubo.tv token for the sandbox context → my calls ran as schaudhri@fubo.tv.
PR #70 (docs/install-walkthrough): docs/install.md replaces dogfood.md;
bootstrap: Cloud Build PERMISSION_DENIED retry (3x45s, only that error),
no restart when pod has no digest, closing text name=${CLUSTER}; 5 new
tests (407). CI pending at checkpoint. Removal section NOT run.
FINDINGS NOT YET FILED/FIXED: (1) Gojira preempted by gke-gmp-system rule-
evaluator (gmp-critical 1e9) when first tenant's Rules CR arrived → needs
a PriorityClass decision (would Gojira then preempt tenants?); (2) IAP
OAuth Admin API: Google docs say shut down 2026-03-19 (new projects
2026-01-19) — bootstrap step 6 brands/clients create likely dead; my 7/26
note says it worked on fubo dev → contradiction, untested; fix = drop
oauthclientCredentials, use Google-managed client; (3) drift scan on
Autopilot reports Autopilot's own mutations (memory ratio, ephemeral-
storage limit 8Gi→4Gi) as drift, capture PR writes them to git; UI shows
raw millicores/bytes.
DESTROY ANSWER given to Shaz: no good destroy. terraform owns 17 resources;
leftovers = per-app GSA+roles (app delete handles, only while Gojira
alive), WIF pool gojira (30-day name reservation if deleted), AR repo apps,
data disk (GKE retains PDs), Cloud Build bucket, front door, repo files.
RECOMMENDED: `make uninstall` (apps via DELETE API → front door → ns gojira
→ terraform destroy; pool kept unless --purge) + periodic stranger test in
a FRESH project destroyed by `gcloud projects delete`. AWAITING SHAZ's
decision on both. NEXT after that: rebuild Fubo dev Gojira from main
(#65-#67 + #70), adopt-rename design (Kyle's ruling), ARM pool, multi-
agent-per-repo design, parallel regional envs. #66 still OPEN.

# CHECKPOINT 2026-09-29 (pre-compact) — DOGFOOD LIVE; hello-app setup PR open

STATE: tenishai dogfood = shaz-learning (personal billing). Cluster
gojira-sandbox (Autopilot, us-central1) via terraform (local state in
terraform/, tfvars project_id=shaz-learning; auth: GOOGLE_OAUTH_ACCESS_
TOKEN=$(gcloud auth print-access-token --account shahzadchaudhri@gmail.com)).
Gojira installed via `CLOUDSDK_CORE_ACCOUNT=shahzadchaudhri@gmail.com
GOJIRA_ENV=~/gojira-dogfood.env make install` (App 4231856 user-owned,
pem gojira-sandbox.2026-07-06.private-key.pem, installation 144832430
covers shaz/hello-app). kubectl context gke_shaz-learning_us-central1_
gojira-sandbox (needs CLOUDSDK_CORE_ACCOUNT env). Cluster registered as
gojira-sandbox. App 1 = hello-app (web, dev, ns hello) → AWAITING_PR:
https://github.com/shaz/hello-app/pull/1 (auto-merge off).
NEXT STEPS (resume here): 1) merge shaz/hello-app#1 (gh, it's Shaz's repo;
acceptance test) → POST /api/v1/apps/1/sync w/ bearer token from secret
gojira-github-app/api_token via port-forward svc/gojira → status active;
2) watch CI on shaz/hello-app (GitHub-hosted, personal free minutes) →
pod in ns hello 1/1 → curl /health via port-forward; 3) write docs/
install.md from the narrated gaps (findings so far: WI binding race → #69
merged?; Cloud Build PERMISSION_DENIED right after API enable → retry
once / preflight probe; bootstrap roll step restarts needlessly on a fresh
cluster (HAVE empty) → skip when empty; terraform state ignore #68); the
walkthrough = the acceptance test; 4) rebuild dev Gojira from main (#65-
#67 not deployed); 5) ADOPT RENAME design (Kyle's 2:02pm ruling: agent name
independent of repo; Gojira renames deployment/SA/namespace + rewrites ALL
overlays: patch targets file+inline, patch-file metadata.name,
images[].name→gojira-app, namespace, generator names; PR body lists
rewrites + out-of-repo secrets to verify; ~1.5 days; Kai flips kobo
mismatch→warning after; kobo #73 ROLLED); 6) ARM pool on the dogfood;
7) multi-agent-per-repo design owed (one contract per repo today).
OPEN PRs: #68 (tf gitignore+lock), #69 (WI depends_on) — green, Shaz
merges. Local Prudence: memory reference_local_prudence (use ~100k ctx).
Kobo #73 reviewed (3 findings; gojira #67 sync API merged for #1).

# 2026-09-29 (dogfood) — TENISHAI DOGFOOD ON shaz-learning IS UP

Shaz's personal project shaz-learning (personal billing, owner =
shahzadchaudhri@gmail.com, which gcloud on spark already holds — use
CLOUDSDK_CORE_ACCOUNT=shahzadchaudhri@gmail.com, never switch globally).
terraform 1.16.4 installed at ~/.local/bin; state LOCAL in terraform/
(gitignored via #68 merged?; tfvars project_id=shaz-learning). Provider
auth via GOOGLE_OAUTH_ACCESS_TOKEN=$(gcloud auth print-access-token
--account …). Apply: 17 resources; FINDING: WI binding raced the cluster
(pool didn't exist) → PR #69 depends_on (green). Second apply completed.
Install: GOJIRA_ENV=~/gojira-dogfood.env make install; FINDING: first
`gcloud builds submit` PERMISSION_DENIED as owner = API-enable propagation;
45s later fine (preflight should probe Cloud Build; install.md must say
"retry once"). Bootstrap roll step: on a fresh cluster HAVE is empty →
needless restart (nit). Gojira pod up, context gke_shaz-learning_us-
central1_gojira-sandbox. GitHub App = July user-owned gojira-sandbox
(4231856, pem at repo root), installation 144832430 now includes
shaz/hello-app (created from examples/hello-app). Dogfood env file:
~/gojira-dogfood.env. NEXT in this run: register cluster (name =
gojira-sandbox), connect hello-app (web, dev, ns hello), merge setup PR,
watch CI on shaz/hello-app, verify pod → then docs/install.md from the
narrated gaps; ARM pool later. KYLE'S NEW RULING (2:02pm): agent name
independent of repo → Gojira renames in the setup PR (deployment, SA,
namespace + rewrite ALL overlays: patch targets file+inline, patch file
metadata.name, images[].name→gojira-app, namespace, generator names);
Kai flips kobo mismatch → warning after. Kobo #73 ROLLED. Design proposed
to Shaz; ~1.5 days; build after dogfood. Dev Gojira image lacks #65-#67 →
rebuild after dogfood.

# 2026-09-29 (latest) — KOBO #73 REVIEWED (changes requested) + gojira #67 sync API

Kai's kobo PR = #73 "Connected repos keep their name, wait on their setup
PR, and are never auto-merged" (+1043, 4 commits, 4035 API tests claimed).
My review (CHANGES_REQUESTED, signed): (1) IMPORTANT parked agents never
resume — sweep only resumes on gojira `active`, gojira status is DB-only,
dev webhook = 0 deliveries in 7d → built gojira #67 POST /api/v1/apps/
{id}/sync (token-gated, sync_app, {status,changed}; 402 tests) and asked
the sweep to call it; (2) IMPORTANT read-then-write resume race across
replicas (PLAUSIBLE; suggest conditional UPDATE/rowcount); (3) IMPORTANT
connected wiring overwrites the repo's service account with agent-{name}
(steps.py:1529; DeployStep expects it) — Kyle's ruling should cover the SA
too; repo_service_account is scanned but display-only. #67 CI watch pending.
Lesson (twice today): `pytest | tail` in an && chain hides failures →
`set -o pipefail` always.

# 2026-09-29 (late) — LOCAL PRUDENCE WORKS: first review on spark → PR #66

review-daedalus.sh on gojira #59 w/ local Qwen3.8-27B (:43793, venv w/
daedalus[mcp] from a fresh clone; details in memory reference_local_
prudence): 29 min, exit 1 = partial (40k ctx cap; server has 131k → use
~100k next), but the review is REAL: list→search→read→targeted tests (33)
→full suite (387)→ruff; verdict LGTM; nit = stale causal comment (uid pin
explained by a theory its own commit falsified) — VERIFIED true in both
steps.py + test_observe.py → PR #66 (comments only, credited to Prudence
local). Two false starts before: stale daedalus checkout lacked --engine
(fresh clone fixed), mcp extra missing, #65 merged mid-run (empty diff).
Shaz declined the codereview-qwen32b adapter. Posting as her needs Rich's
identity; for now post under Ember w/ attribution.

# 2026-09-29 (night) — KYLE'S RULING on connected-repo identity; Prudence path

Kai's reply (via Shaz): correction — Friday's failure WAS the inline-patch
crash (#57); rename+auto-merge = the next risk, not the cause. Kyle's
RULING: a connected repo keeps its own name — kobo takes the base
Deployment's name as agent name + namespace + SA, locks the form, refuses
a mismatch ("connect under that name or rename in the repo first"). The
platform never renames. Kai takes all three kobo changes (no auto-merge
for connected, awaiting_pr = paused, name lock) as ONE kobo PR. #58/#59
moot → Kai closes. #65 MERGED 16:46.
PRUDENCE PATH (my recommendation, Shaz to decide): connect as pr-reviewer
(repo identity) — both overlays keep matching, dev ns pr-reviewer already
has her secrets, prod untouched, no SDK bump (port/health in contract).
Steps: Kai's kobo PR → close setup PR #35 + delete gojira app 17 → connect
as pr-reviewer → review setup PR (hand-add config_from: [pr-reviewer-env]
until #61) → merge → retire ArgoCD pr-reviewer-dev app (global-infra-
tools, Kyle/Rich) → dev cutover. Prudence-local-review experiment: agents'
review-daedalus.sh on spark w/ local Qwen3.8-27B (venv at scratch/
prudence-venv, daedalus[mcp] from a fresh scratch clone); run on #59 in
progress. Shaz declined the codereview-qwen32b adapter (assessed: single-
turn comment generator, BLEU-evaluated, no tool use; not for Prudence).

# 2026-09-29 (evening) — bab #2 merged; Kyle note sent; PR #65 overrides.hpa

bab #2 MERGED (bab main green on spark); local bab main synced. Shaz SENT
the Kyle note (three kobo changes). PR #65 feat/hpa-overrides:
overrides.hpa {min,max,cpu,memory} any subset; class decides HPA exists;
agent ignores; HPA opinion gets optional target_memory_percent; sizing PR
(open_override_pr) now carries existing hpa through the rewritten block
(finding #9's shape prevented); junk ignored; 399 tests; goldens = header
comment only. CI watch pending. NEXT: parallel regional envs (design
needed: promotion gating with two "last" envs); docs/install.md; Prudence
identity decision (await Kyle's reply).

# 2026-09-29 (end) — #60 + #64 MERGED + DEPLOYED to dev; local main reset

Shaz merged #60 (adopt refuses auto-merge) and #64 (bootstrap rolls on new
digest + exact-tag digest lookup). Local main was still Friday's pre-
rewrite history (56 duplicate-hash commits, nothing unique) → reset --hard
origin/main. `make install` deployed main: bootstrap compared registry vs
pod digest, rolled, pod 1/1 on 0ca37e… == registry dev, /healthz 200. Dev
Gojira now carries: port+health contract, adopt auto-merge guard, exact
digest lookup, self-rolling bootstrap. Open for Shaz: bab #2 (green).
Drafted the Kyle note (three kobo changes: no auto-merge for connected
repos, awaiting_pr = paused-for-a-person, repo name = identity or refuse)
— Shaz to send. NEXT: HPA bounds/metrics in overrides; parallel regional
envs; Prudence identity decision + repo PR; docs/install.md walkthrough
(still not started); kobo changes with Kyle.

# 2026-09-29 (later) — #63 MERGED + DEPLOYED; bab #4 merged, #2 rebased green; PR #64

#63 (port+health contract) MERGED by Shaz; main's post-merge run GREEN on
spark (first green main since 9/23). #60 rebased → GREEN, awaits merge.
bab #4 (runner) MERGED; bab #2 (example profile) REBASED over it (ci.yml
conflict resolved: runner lines + example.fga) → GREEN, awaits merge.
DEPLOYED main (#63) to dev via make install from the patched bootstrap:
new step 5 compared registry digest vs pod digest, printed "rolling",
restarted, pod 1/1 on the true :dev digest 0b9816…, /healthz 200.
FINDING: gcloud `tag:` filter is a PREFIX match (tag:dev → 3 rows;
tag:219 → 2196912) → gcp.py image_digest (#47) would return a newline-
joined "digest" on collisions (rollback pin broken/wrong). PR #64: exact
tag selection in gcp.py (value(tag,version) + pick) + bootstrap awk exact
match + the roll step; 392 tests. Awaits CI on spark + Shaz.

# 2026-09-29 — SPARK IS THE CI RUNNER (arm64, self-hosted); #63 GREEN

Shaz couldn't open the org runner page (he IS an org owner — browser
account?). Didn't matter: repo-level runners need only repo admin, which
my gh token has. Registered spark-gojira + spark-bab (labels spark,arm64,
linux) as USER systemd services (~/.config/systemd/user/gh-runner-*.service,
dirs ~/actions-runner-{gojira,bab}, runner 2.337.0 arm64). Repo variable
CI_RUNNER=spark on both; workflows `runs-on: ${{ vars.CI_RUNNER ||
'ubuntu-latest' }}`. Lessons (all bit): (1) systemd user manager env
carries the NVIDIA PYTHONPATH → UnsetEnvironment=PYTHONPATH in the unit;
(2) Environment=PATH must include ~/google-cloud-sdk/bin (kubectl);
(3) KillMode=process left the old Runner.Listener alive across restarts
with stale env — removed; (4) hashicorp/setup-terraform's node wrapper
needs a node on PATH → terraform_wrapper: false; (5) _work persists →
rm -rf .venv per job; (6) generated contract-check snippet used bare `pip`
→ now `python3 -m pip` (goldens changed 1 line ×12) and the test puts its
own interpreter first on PATH + passes HOME. gojira #63 = ALL GREEN on
spark. bab PR ci/spark-runner opened. #60 rebased; its CI stays red until
#63 merges (workflow change lives there) → rerun after.

# 2026-09-28 (later) — LOOSE COUPLING RESTATED; port+health contract PR

Shaz: "original design goal — deploy, measure and rightsize apps without
developers needing to write a bunch of circle/github nonsense"; Kai couples
tightly; Shaz blames himself for not insisting. I owned my part: APP_PORT
constant + /health opinion (June) + agent /healthz (#38) were mine. Saved
memory feedback_gojira_loose_coupling. Segment-proxy thought experiment
(fubotv/fubo-segment-proxy: Go, helm, CircleCI, 4 clusters US/EU, canary,
HPA 30-500, NodePort+NEG, /alive, ~50 env, tainted pools): gaps ranked —
health path/port (NOW a PR), HPA bounds+metrics in overrides, parallel
regional envs, LB/NEG exposure, canary, app IAM grants, adopt-from-helm;
plus WIF pool in fubotv-prod = access channel. All general web needs, none
agent-shaped. PR feat/port-and-health-contract: `port:`+`health:` in
gojira.yaml (class defaults), effective probes at render, adopt infers
containerPort + httpGet.path, admin_port vs real port, docs; 391 tests.
#60 REBASED onto the rewritten main and force-pushed (own branch).

# 2026-09-28 — PRUDENCE ON KOBO handed to us + GOJIRA MAIN FORCE-PUSHED (3rd rewrite)

Kyle handed "prudence on kobo" to Shaz+me. FACTS: kobo app prudence =
fubotv/infra-agent-pr-reviewer (the FUBO Prudence, fubo-agent SDK — NOT
Rich's tenishai daedalus Prudence). gojira app 17: awaiting_pr, auto-merge
OFF (my DB flip), setup PR #35 open w/ my changes-requested; no reply from
Kyle. Dev cluster: ns pr-reviewer runs the OLD deployment (161d, ArgoCD
pr-reviewer-dev auto-sync prune+selfHeal on k8s/overlays/dev, ns
pr-reviewer); ns prudence EMPTY. Prod: ns prudence on fubo-ai-agents-prod,
image b0471a3 via k8s/overlays/prudence-prod (apply path unverified; no
argocd manifest for prod in repo; PROD READS DENIED by classifier).
Repo pins fubo-agent>=0.8.0 (May image) — /healthz alias came in SDK 0.9.1,
fleet on 0.9.3 (0.9.2 zombie bug), latest v0.11.0 → agent-class probes
need an SDK bump + rebuild. Dev secrets are plain Opaque hand-made
(pr-reviewer-secrets ×4, github-private-key), not bao/ESO.
KOBO MODEL: DeployStep verifies deploy==ns==agent.name → the k8s identity
MUST be 'prudence' (rename repo objects) or agent named pr-reviewer.
ConnectedWireStep already does PR-not-push for connected repos, but
clients.py:673 still sends auto_merge_setup_pr=True for everything;
_wait_active treats awaiting_pr as "keep polling until POLL_TIMEOUT_S then
raise" → a human-reviewed PR = timeout failure. Kobo needs: auto_merge only
for room-owned repos; awaiting_pr = paused-for-a-person with PR link;
resume on merge. BuildStep polls the repo's gojira-ci run; kobo-authored
files = serviceaccount.yaml + gojira.yaml contract edit.
PLAN (proposed to Shaz): (1) repo PR: rename objects → prudence, retarget
dev + prudence-prod overlays (prod images name → gojira-app), SDK ≥0.9.3
w/ scratch-venv validation, hand-add config_from [prudence-env]; (2) gojira:
merge #60, rebase all PRs (main rewritten), #61 later; (3) kobo: the three
changes above (Kyle/Kai or us); (4) ArgoCD: retire pr-reviewer-dev
Application in global-infra-tools when Gojira owns dev; (5) secrets → bao
k8s/prudence + ExternalSecrets (Shaz writes secrets); (6) dev cutover,
decommission ns pr-reviewer; (7) prod = later milestone (register prod
cluster in gojira).
GOJIRA MAIN FORCE-PUSHED by kmgreiner3 2026-09-28 16:39Z (c0f4e93→2e65153):
identical tree, ALL history rewritten — his one kgreiner@fubo.tv commit
(#35, 863dd52) now noreply. Unannounced. Every open PR (#58 #59 #60) and
every clone diverged; my branches need rebase. Third unannounced rewrite
(daedalus 9/23, now gojira). Kobo NOT rewritten (local just 396 behind).
CI still dead (billing). Walkthrough PR still not started.

# 2026-09-25 (later) — PRUDENCE-VIA-KOBO INCIDENT → FINDING #22 + dev on #57

Kobo onboarded fubotv/infra-agent-pr-reviewer (PRODUCTION Prudence's repo)
as gojira app 17 'prudence' with auto_merge_setup_pr=TRUE (kobo sets it on
every onboarding). generate_artifacts crashed KeyError 'path' = inline
kustomize patch (target+patch, no path) in the repo's dev overlay — exactly
Kai's #57 (merged 12:44, NOT deployed). Proved against the real overlay:
491dda7 code crashes, main carries both patches. DEPLOYED main to dev via
make install — apply was a NO-OP (same :dev tag; #37's shape on our own
install) → kubectl rollout restart → new pod on the new digest. TODO:
bootstrap pins digest (tags list, not describe — #47).
STOPGAP: flipped apps.auto_merge_setup_pr=0 for id 17 in the dev sqlite.
Re-kicked → setup PR fubotv/infra-agent-pr-reviewer#35 opened, NOT merged.
PR #35 would break prod: base Deployment renamed pr-reviewer→prudence,
overlay patches (dev + untouched prudence-prod) target pr-reviewer, env
configmap wiring lost (adopt reads base only). Posted CHANGES_REQUESTED
review w/ two ways out (re-onboard as pr-reviewer, or hand-fix overlays).
gojira PR #60: adopt mode refuses auto-merge (finding #22, 389 tests).
gojira issue #61: overlay-added needs not inferred + rename orphans patches.
Commented on #59 re deploy ordering (#58 revert would drop the fix).
CI on tenishai is DEAD: GitHub billing/spending limit ("recent account
payments have failed") since ~9/24 — all jobs fail in 2s with no steps.
Shaz/Rich to fix billing or add a runner variable. Lumbot crashloops:
ModuleNotFoundError fubo_agent (agent-lumbot image; Kyle's side).
Walkthrough PR (install.md + vocabulary + sibling rule) NOT started yet.

# 2026-09-25 — FIRST `make install` ON DEV (values path proven) + #50 live

#50 (usage read API) merged → `make install` from local gojira.env ran the
full path on fubo dev: Cloud Build, secret configured (api_token/webhook
preserved), gojira-install ConfigMap CREATED (auth=true fubo.tv, labels
{fubo.tv/team: sre}, drift=true, agent image ''), configmap+deployment
configured, rolled (new pod 1/1), IAP member re-asserted (idempotent).
VERIFIED in-pod: effective env matches the old hand-patched config; OTEL
endpoint (imperative, argus collector) SURVIVED the apply; /healthz 200;
/api/v1/apps/1/usage 401 no-token / 200 with token; iris profiles=[] (no
producer on this cluster yet). The permanently-dirty deploy/ era is over:
deploy/ clean, gojira.env is the operator file. Commented on #50.

DAEDALUS #37 (5k rollup) WAS MERGED by Rich 2026-09-25 00:42Z after
Prudence-bot review only (round 1 changes-requested → round 2 LGTM with
mutation checks + squash-tree verification); #79 merged 11 min later; #83
SECURITY.md. Governance finding for Shaz: second owner-less decision on
his repo in 3 days; private repos can't have branch protection → owner
sign-off must be a spoken norm. My reviewer agent on #37 diffs pending.
HANDED to the daedalus-04 tab (Ember, daedalus session): it re-verified the
four findings, will file them as signed issues itself (one voice per repo —
I stay clear of daedalus). It also reset Shaz's local daedalus clone to the
rewritten main WITH Shaz's decisions relayed today: agora#19 purge = go
(archive taken), agora#33 clean-room paragraph confirmed + CLOSED. My
pre-rewrite bundle stays in ~/Documents as a second provenance copy.
Shaz's remaining loose end: bab #2 merge.

# 2026-09-23 — DAEDALUS REWRITE FOUND + NAME CLEANUP PRs (gojira #55, bab #2)

Rich's overnight note: he + Kyle (both still at Fubo, "just in case") are
open-sourcing techne/liminal/cabal/daedalus/agents (epic techne#1003).
FINDING: daedalus main FORCE-PUSHED by kmgreiner3 2026-09-23 03:21 UTC —
full-history rewrite (kgreiner@fubo.tv → noreply; blanket fubo→client over
every blob incl. Shaz's July 2 design commit, so #66's paragraph was decided
by the script before Shaz answered). Not announced; #66 still open. Other
four repos intact. Pre-rewrite history bundled: ~/Documents/daedalus-pre-
rewrite-main-2026-09-22.bundle (+.log). Shaz's local daedalus main still
has original hashes — DO NOT reset until he decides. Advice to Shaz: ask
for a written record in #66 + no rewrite on the other repos before the
three-way hour. Shaz drafting reply to Rich.

#49 #52 #53 #54 merged. NAME CLEANUP as visible commits (no rewrites):
gojira PR #55 (chore/no-employer-names; 2 commits — first pass was
incomplete: grep truncated by head + edit script aborted while the chain
opened the PR; second commit completes it; 380 tests). bab PR #2 (chore/
example-profile off origin/main — local main was the stale July prototype,
now ff'd): fubo profile → example fleet (acme/platform/data, atlas/beacon,
alice..dave), model regenerated (23/12/2 fga assertions), CLI okta-url has
no employer default, 22 pytest. Real Fubo profile saved to ~/Documents/
bab-fubo-profile-2026-09-23/. Both PRs await Shaz; CI watched.

NEXT: item 6 docs/install.md (verbatim walkthrough; ideal = follow it on a
fresh sandbox, needs Shaz's terraform); WIF attribute-condition re-check;
kaminos has no Fubo refs (checked 9/22). gojira.env (local) holds live
dev values; `make install` on dev not yet run (rolls the pod — Shaz's call).

# 2026-09-22 — SPIN-OFF INVENTORY + STRANGER-INSTALL DAY ONE (PR #51)

Shaz: tired of Fubo, wants a spin-off for income while the Primer matures.
INVENTORY at ~/Documents/tenishai-inventory-2026-09-22.md (local, sensitive):
tenishai = Shaz + Rich DiMartino (techne/cabal/liminal/agents/ergon/skills) +
Kyle (kobo/argus/ladon); Kyle committed from fubo.tv into gojira/daedalus/
kobo/cabal; bab carries Fubo's real topology. Market verified 9/22: nobody
sells self-hosted fleet governance w/ per-action approval (Guild.ai/Obot
nearest, HumanLayer pivoted); no rightsizing vendor opens PRs (AWS ref arch
does); no GKE-only deploy platform. Recommendation: IP agreement + Rich/Kyle
talk first, consulting bridge, OSS as funnel, don't leap.

FINDING #21: bootstrap.sh never minted api_token → fresh installs unbootable
since 8/4 (key hand-added on live cluster). Branch fix/stranger-install-
day-one, PR #51 open: api_token mint/preserve + printed retrieval, scripts/
preflight.sh (ran live green), tests/test_deploy_contract.py (negative-
checked), first repo CI (.github/workflows/ci.yaml), docs truth pass,
ar_location knob deleted, Makefile amd64. 368 tests. CI GREEN after two
runner-only failures: ruff 0.16 widened defaults (pinned select E4/E7/E9/F)
and bare `pytest` can't import tests.fakes (pythonpath=. in pyproject). Local deploy/ files
restored w/ shaz-test values (ar_location line removed there too).
feat/observe-step branch untouched (2 commits ahead, pushed).

#51 MERGED. PR #52 (feat/install-values) OPEN: gojira.env(.example) +
scripts/env.sh + install.sh + `make install` (`make dev` = venv now);
configmap interpolates knobs from a bootstrap-built gojira-install ConfigMap
(deployment envFrom, optional); auth.enabled derived = hostname AND allowlist;
config.py string validators; 372 tests; server dry-run on dev cluster OK.
deploy/ is CLEAN now — the shaz-test dirty files are retired; local
gojira.env (gitignored) holds the live dev values (App 4349575, IAP fubo.tv,
labels). NOT run live: `make install` rolls the dev pod — Shaz's call.
PR #53 (feat/dns-access-at-registration): register_cluster enables DNS
access before the probe; terraform control_plane_endpoints_config (provider
>= 6.11, verified in release notes); docs stop claiming a can-i probe. CI green.
PR #54 (feat/hello-app-and-app-doc): github-app-setup.md rewritten for any
org (org-owned warning, secret handoff); examples/hello-app (8080,/health,
/metrics, smoke-tested). No code. dogfood.md untouched on purpose (#52 edits it).
ALL THREE (#52 #53 #54) AWAIT SHAZ. Independent branches off main; merge in
any order (#52 and #53 both touch docs/dogfood.md + design.md — expect a
trivial rebase on whichever lands second).
NEXT: item 6 docs/install.md — verbatim walkthrough replacing dogfood.md,
AFTER the three merge. Ideal: follow it myself on a fresh sandbox project
(needs Shaz's terraform) — the acceptance test. Then: item 7 is done, item 8
done; remaining from audit: WIF provider attribute-condition re-check
(second GitHub org in one project silently rejected).
OLD NEXT: DNS access at registration
(service.py register_cluster) + terraform dns_endpoint_config; de-Fubo
docs/github-app-setup.md + examples/hello-app; docs/install.md verbatim
walkthrough. Acceptance: a non-author installs without asking a question.

# 2026-09-17 (part 2) — ROLLBACK PROVEN LIVE + FINDING: describe's hidden toll (#47)

Shaz dogfooded Roll back on cinder: FIRST CLICK found #47 — gcloud
artifacts docker images DESCRIBE also queries Container Analysis
(containeranalysis.occurrences.list, in NO AR role) for its vuln summary;
digest lookup paying a security report's toll. artifactregistry.admin
still denied! Fix: tags list --filter=tag: --format=value(version) —
pure AR, reader-and-up. ALSO swapped in generated CI (_deploy.j2/#37)
which works only by environmental accident (runner gcloud skips the CA
call). PR #47 merged+deployed; 360 tests. SECOND CLICK: full success —
pod on tag@digest receipt, honesty message rendered, EVENT app.rolled_back
payload carries {environment, sha, image, actor: schaudhri@fubo.tv} =
who-clicked ledger's first real entry. Revert-PR half still UNTESTED
(optional completion; next push/Redeploy re-advances by design).

ARGUS THREAD (same day): Kai accepted everything; his corrections —
URL defaults are localhost-only today (he builds in-cluster derivation);
cluster NAME accident: gojira says fubo-dev (my July label), argus tenant
= ai-agents-dev (real GKE name). RESOLUTION: gojira renames (blast radius
verified: DB row + 2 gojira.yaml refresh PRs + kobo org config line;
CI/WIF/history untouched), argus stays, Mimir history safe; argus_cluster
= temp bridge field, dies at convergence. PRINCIPLE to codify: register
clusters under their real GKE names — infrastructure is the only naming
authority (registration form should default name = gke_cluster).

MY QUEUE: ObserveStep (vs config-absent) + GET /api/v1/apps/{id}/usage
(BEFORE Sept 25) + registration-name default + rename ceremony w/ Shaz.
Cinder's Provision-again refresh STILL pending (pre-#34 manifests!).

# 2026-09-17 — ARGUS OBSERVE DESIGN AGREED (born observed) + sizing migration dated

Kai's Approach A accepted w/ my 4 amendments, ALL agreed (collab thread
ts=1789681170.611259): (1) ObserveStep ephemeral → Provision-again
re-observes (argus observe must be idempotent — Kai to confirm);
(2) config = clusters MAP not scalar, absent = skipped; (3) job_image
digest-pinned + ttlSecondsAfterFinished + inline contract + GOJIRA_CONTRACT
sha, argus summary → ledger resource_ref; (4) metrics boundary WITH EXPIRY:
argus owns dashboards/alerts/SLOs from first observe; GMP feeds sizing
until parity, then `argus feedback` = single producer, sizing agent
RETIRES. One-producer rule throughout.

SIZING MIGRATION FACTS (from Kai): argus already collects per-pod cpu/mem
+ requests/limits + restarts/OOMs; `argus feedback` bridge exists (print-
only), shared wire fixture guards format both repos. Mimir metric names ≠
classic cAdvisor names (their testing: classic names return NOTHING) →
bridge over retune. Retention: 30d promised NOT enforced, dev argus live
since 9/9 → parity window Sept 25→Oct 9 (meaningful from ~Sept 23, 14d
windows); asked Kai to make retention enforced-and-alarmed. Parity bar:
recommender makes SAME CALL from either summary (binding), ±10% p95
(diagnostic); per-pod vs per-container divergence expected only on
sidecar apps — enumerate multi-container apps up front.

DIVISION: Kai — published digest-pinnable argus image + ProvisionPerses
in-engine. ME — ObserveStep (ephemeral, per-env, failure-isolated: observe
fail ≠ provision fail, recorded loudly) + clusters config + faked-kubectl
tests + config-absent skip. Independent tracks; I can land vs config-absent
first. NEW MEMORY: feedback_translate_agent_speak (two registers always —
Rook 7/21 + Kai today both threw Shaz; plain English for humans, unprompted).

#45 ROLLBACK BUTTON merged + deployed 9/17 (357 tests; Deploy history
renders on app pages from the runs API). Live rollback test pending —
CINDER is the dogfood target (iris is PRODUCTION, clio is Yan's).
MY NEXT BUILDS: ObserveStep + GET /api/v1/apps/{id}/usage (before Sept 25).

# 2026-09-16 — BAB IS BORN (fleet guardrails on OpenFGA)

Design chat → repo in one session. tenishai/bab (باب, door; Shaz named it,
tired of Greek). Actor → action → object, verdicts allow/approve/deny.
Catalogue generates the OpenFGA model (5 relations/action/level incl. two
ceilings); approvers = owning team's members (Shaz: membership = approvers,
no leads); identity = email. Three tuple sources: Okta (infra-okta-viewer
JSON API, Go app, NO MCP), topology (YAML now; Gojira has NO list endpoints
→ add GET /api/v1/topology), console grants. SHAZ DECISIONS: DB-backed not
git; runtime calls OpenFGA live, fail-closed; ceiling in increment one +
four-eyes on ceiling edits; console in kobo only after data model + API,
THEN bring Kyle in; store = new db on sre-agents AlloyDB (dev first).
main = reviewed prototype; PR #1 = package (22 pytest, fga suite 5/23/12/2,
`make demo` = real OpenFGA in Docker, six live verdicts + degraded path).
Branch protection unavailable on private tenishai repos (needs Pro).
LESSON: deny tests must be set assertions (list-objects empty) — point
checks missed an injected over-broad grant. MEMORY FIXES: LadybugDB axed by
Rich+Rook; Archivist = AlloyDB (graph_nodes/edges); Shaz's ePals neo4j authz
story saved in user_background.md.
NEXT: Shaz merges #1 → increment 2 (deploy: OpenFGA on dev cluster + AlloyDB
db + sync CronJobs + Gojira topology endpoint) → 3 (SDK Door.decide, Iris +
Cinder) → 4 (kobo console: what-can, playground+explain, grants, pinned
assertions). Show Rich + Kyle `make demo`, not the doc.

# 2026-09-14 (part 2) — WIZ CRITICAL CLOSED + THE FULL AI REVIEW LOOP + #43/#44/#45/#46

WIZ CRITICAL on gojira Deployment: 3 OS CVEs (libc6 CVE-2026-5450 Crit,
libssh2 2x High) — base python:3.12-slim lags Debian security; Dockerfile
now apt-upgrades in build (#46, merged, deployed). Pod sealed: runAsNonRoot,
seccomp RuntimeDefault, no-priv-esc, caps dropped, readOnly rootFS +
emptyDirs /home/gojira + /tmp (drift sweeps proven under seal). IAP verified
live everywhere except HMAC /webhook (by design). Verified in-pod: libc6
deb13u4, libssh2 deb13u2. GOTCHA: deploy/deployment.yaml has image
placeholder — apply -f alone → pulls docker.io/library/gojira; use set
image / kustomize. Kobo's Wiz item for Kyle: http://kobo.apps.fubotv.net:80/
oauth OPEN on plain HTTP. Compliance comment drafted, Shaz posts + rescan.

KAI LOOP (Kyle's AI): fixed both my review findings same-day (#43 bool+
dup-port one condition; #44 500s-reach-counter + segment-boundary exempts),
found + fixed the SAME crash-counting hole in kobo's twin middleware
(57eac72), reviewed AND squash-merged my kobo #7 (verified head_sha on both
real backends — the thing fakes can't prove). I re-verified both PRs
empirically, approved; Shaz merged #43+#44. Gojira on fubo = main @ 5f91b16:
gateway class, RED live (http_requests_total counting), tracing → Kyle's
argus-gateway-collector (env survived my rolls via 3-way merge — set
imperatively, never in manifest). Argus ns live on dev cluster since 9/9.

OPEN: PR #45 ROLLBACK BUTTON awaits Shaz review/merge (built earlier today:
digest receipts, deploy_history from runs API, who-clicked Event, revert-PR
honesty; after merge → rebuild+roll → live-test on clio or a throwaway).
ArgoCD decided: layers-not-rivals, keep our apply+drift-capture doctrine,
sizing loop is engine-proof (actuates via git); optional Argo Application
emitter if org pressure arrives. Kobo roll pending Kyle-side (delivery fix
+ 57eac72).

# 2026-09-14 — KYLE'S PRs REVIEWED + KOBO #7 REBASED (catch-up day)

Reviewed Kyle's gojira PRs (both ~1wk old): #43 gateway class (Recreate/
1-replica/no-HPA for SQLite state + admin_port second listener/Service) —
2 probed edges posted: admin_port:true slips isinstance(int) (bool IS int)
→ containerPort:True breaks deploys; admin_port==APP_PORT = the exact
shared-listener accident the field promises to prevent (server dry-run:
warning only). #44 RED metrics + UNDISCLOSED OTel tracing module — big
confirmed find: unhandled 500s never reach http_requests_total (exception
propagates through middleware, inc skipped) = RED without the E; fix shape
posted (try/except inc 500, re-raise). Both reviews on GitHub, signed.
ASK KYLE to fix before merge; Shaz sent him the note (reviews + finding
#20 heads-up + kobo #7 review request).

KOBO #7 (delivery_state follows setup PR when own run fails — the finding
#20 UX tail): rebased onto Kyle's moved main (+14k lines: kobo got
metrics/tracing too; his slack-manifest test failure fixed), clean rebase,
2923 tests green, MERGEABLE/CLEAN. Kyle to review (his codebase).

PENDING ON OTHERS: Kyle — fix #43/#44 edges, review kobo #7. Me — nothing
until he responds. Test agents general-pinochet/gemini-one torn down (ns
empty); clio + gojira pods healthy.

# 2026-08-27 — FINDING #20: CONTRACT/MANIFEST RACE (never checkpointed)

Clio's 'connect slack' crashlooped 10min: kobo's gojira.yaml push + Gojira's
setup PR land as TWO commits 14s apart; CI deployed the first (new image,
old deployment, no slack secret). Gojira #42 (merged, deployed): workflows
carry GOJIRA_CONTRACT = sha256 of CANONICAL gojira.yaml (parse→sorted JSON,
so machine reformats don't trip); verify step after checkout fails in ~10s
pre-build on mismatch. stamp_contract() for sizing PR's textual edits.
Test runs the generated bash FOR REAL. Issue #41 closed. Proven live on
general-pinochet: kobo push failed 11s at verify, setup PR green behind,
pod 1/1 zero crash — clio's incident replayed and caught. Bonus learning:
GitHub concurrency queue-cancels older PENDING run (cancel-in-progress
false only guards running) — kobo's contract pushes often die that way too.
Kobo delivery chip then lied ('didn't go through') → kobo PR #7.

# 2026-08-22 — QWEN3.8 TIRE-KICK + WORKER DOGFOOD LIVE

Qwen3.8-27B-UD-Q4_K_XL now the 8888 brain (unsloth; 2.5x faster serving
than vanilla llama-server — MTP head). Vision REQUIRES the :8891 sidecar
(unsloth run has no mmproj support; mmproj-F16 in ~/models/Qwen3.8-27B/).
Full tire-kick results appended to verity-vision-design-2026-08-18.md:
OCR perfect, dual-replay adjudication 3/3 thinking (262s) / 2/2 no-think
(19s, THE routine tier). Tool calls: proper on both stacks.

LOCAL-WORKER DOGFOOD (Qwen3.8 brain): 2/2 baseline — strict-required env
vars EXACTLY right (3, better precision than the old loose-question 8),
class count 16 correct vs live file. ~2-4min/task (thinking tax; old coder
~1-1.5min). Next tune: no-think for worker dispatches.

NEXT SESSION HEADLINE: ember-recall index design (agreed w/ Shaz) — data
model, what gets indexed (memory/observations/techne cards/distilled
transcripts), dispatch integration (worker gets task-scoped cards via
:8890 embedder — 'give it a librarian, not the handbook'). Then A/B:
worker accuracy 0/2/fat-context cards.

WART: skillz PRs #227 (local-worker) #229 (ember-say) STILL OPEN — repo
branch drift left dangling symlinks; local-worker + skills/local-worker
now MATERIALIZED as real files in ~/.claude (re-symlink after merges).
NUDGE SHAZ TO MERGE.

# 2026-08-18 — LOCAL STACK ARC + VERITY VISION DRAFT

Spark organism now: Qwen3-Coder-30B-A3B :8888 (brain, tool-calling proven),
Qwen3-Embedding-0.6B :8890 (systemd ember-embed.service, linger on, 3/3
semantic recall over MEMORY.md), Piper alba :speaker (ember-say, PR #229),
local-worker skill (PR #227) — BOTH PRs AWAIT MERGE. whisper.cpp on disk,
no mic yet.

NEXT SESSION: Verity-gets-eyes prototype. FULL DRAFT AT
~/src/git/infra-sre-skillz/notes/verity-vision-design-2026-08-18.md
(uncommitted). Plan: Qwen3-VL-8B sidecar :8891 → Playwright loop vs Gojira
dev UI → local-VLM verdict vs my verdict, agreement rate decides the tier.
Then Verity design convo (6 open questions in the doc — tenant-vs-CI, IAP
auth for pod agents, findings channel, golden-screenshot aging).
Also queued: ember-recall index design (my rec before vision arrived),
Voxtral TTS experiment. Qwen3.8-27B (Aug 13, vision mmproj) = benchmark
fallback only, dense/slow.

# 2026-08-13 — THE 0.9.2 INCIDENT (probe truth-teller) + fleet on 0.9.3

Iris crashlooped the moment her refresh PR #23 merged (agent-class http
probes armed) — NOT the probes' fault: SDK 0.9.2's zombie detection
(IE-5801, rook's 8/6 OOMKill outage) read last_ping_pong_time, the
BUILTIN transport's gauge; fleet runs websocket-client transport
(WebSocketApp.last_pong_tm) → every healthy session read never-ponged →
'stale' 120s after connect → /healthz 503 chronic, invisible until
probed. Cinder was 503ing too, unprobed. BOTH were on 0.9.2 unnoticed
(pip resolved it 8/7; __version__ said 0.9.1 — desync). sherlock safe
on 0.8.7 but one rebuild from the trap (http /health liveness).

Fixed: fubo-agent-sdk PR #76 = v0.9.3 (fallback to last_pong_tm;
zombie-from-birth still caught both transports; 3 transport-faithful
tests — 0.9.2's Mocks never modeled real WebSocketApp shape, HOW it
slipped). Prudence LGTM (self-retracted her one Important — traced the
wrapper). id()-reuse nit noted twice, accepted. Field report on IE-5801
(comment 386575). IE-5801+5802 both Backlog/unassigned — monitor 4
NEVER EXISTED; no agent notification channel in either project (only
ClickHouse→Slack prod, LadybugDB→email dev). Shaz's insight validated:
consumption is Datadog kube-state restarts + daily findings digest —
liveness-restart path plugs into EXISTING consumption; monitor 4 stays
unbuilt.

OPERATOR LESSON (mine): emergency kubectl patch (tcp revert) collided
with CI's strategic-merge apply → 'may not specify more than 1 handler
type' × 20 retries. Imperative side-edits and apply don't share field
custody — the right emergency lever is the overlay-patch idiom (drift
capture's shape). Unwedged by patching live to the git shape, then
dispatch. ALSO: my 8/7 manual digest pin made today's tag-form apply a
real spec change → cinder rolled herself; workaround retired itself.

STATE: iris + cinder 0.9.3, 1/1, http /healthz probes live on iris,
slack_connected iris=1 cinder=1 (read past the 120s window). NEXT:
cinder Provision-again refresh NOW SAFE (brings digest pins, concurrency,
readiness+Service). Stale iris drift PR #12 to close. Daily SRE findings
report (dd-monitors noise) shared by Shaz — 29/31 CRITICALs are no-pri
Datadog monitors; context for all alerting design.

# 2026-08-12 — THE KYLE ARC + PLATFORM CONVERGENCE (PRs #32–#38)

Gojira on fubo now runs main @ #38. The arc: delete-dogfood findings → 
Redeploy button + setup_pr_url clear (#32), Provision-again button (#33 — 
finding: healthy apps had NO re-provision surface; Shaz's delete+reconnect 
on cinder 7/29 was the platform's fault, not his). KYLE (kmgreiner3, Kai 
driving) arrived: #34 agent-class /healthz readiness + programmatic 
onboarding API (bearer-gated, auto-merge for machine-authored repos, 
five agents provisioned via his 'assembly room'/kobo; DB self-migrates), 
#35 provision-kick API route. Sequencing dance held: SDK v0.9.1 
(fubo-agent-sdk#72, repo RENAMED from infra-agent-sdk; parallel Ember 
had shipped 0.9.0 + /metrics endpoint — pull before branching in shared 
repos!) added /healthz alias; fleet rebuilt BEFORE #34 merged. 
api_token ceremony: secret key + deployment env + configmap line 
(Shaz ran — classifier blocks me on secret writes); API now 'bearer 
token required' = keyed. #36 sizing-suite TIME BOMB (golden fixture 
reported_at 2026-07-06 aged out of now-30d history window on 8/5 — 
suite had an expiry date). #37 digest-pinned deploys (finding: Redeploy 
on unchanged commit rebuilds under SAME sha tag → apply no-ops, pods 
stale; cinder needed manual digest pin; fix: image:tag@digest, verified 
kustomize 5.4.3; 'tag = functionality for humans, digest = receipt' — 
framing Shaz loved, feeds rollback design). #38 agent liveness tcp→http 
/healthz (iris's 7/26 hand-edit lesson, now class doctrine; trade-off: 
prolonged Slack outage = ~1/min restarts, kubelet backoff caps it; 
cross-reviewed by LOCAL QWEN on :8889 — LGTM; pattern worked, reuse).

IRIS + CINDER: on SDK 0.9.1, /metrics 200s, both 1/1. NOT yet refreshed 
to post-#34 manifests — next Provision-again + merge each brings: 
concurrency block, /healthz probes (readiness+liveness), digest pins, 
generated Service. GitHub ate iris #22's merge-push CI once — dispatched 
by hand (gh workflow run = what Redeploy does).

ARGUS: Kyle's observability platform (tenishai/argus, Mimir/Loki/Tempo/
Perses, 'observability arm of gojira', reads our contract, feeds 
/api/v1/usage, defers to sizing-agent as single evidence producer). 
Sit-down now FOUR topics: item-7 multi-tenant, Argo positioning, 
Argus-vs-GMP metrics, probe-override vocabulary.

OPEN: runtime panel design (pods ready/desired on app page — proposed, 
no verdict), purge design (teardown PR + WIF binding drop — proposed), 
webhook URL+secret handshake (Shaz), Sherlock2 fork (Shaz), 422-before-
401 schema oracle on POST /api/v1/apps (accepted follow-up), 
EnsureWifProvider attributeCondition check, who-clicked ledger, sizing 
agent on fubo, sandbox teardown. Qwen review endpoint: 127.0.0.1:8889 
(unsloth, Anthropic Messages dialect works).

# 2026-07-27 — CINDER ALIVE (oldest debt paid) + delete-flow findings

CINDER deployed via Gojira: pre-flight caught her two remaining twins
(ES paths kv/sre/cinder/* → k8s/cinder one-secret-two-fields, her PR #26;
port 8086 → 8080 fleet contract — probe would have killed her forever),
setup PR #25 (adopt inferred config_from: [cinder-env] — finding #18's
first outing — plus full contract) merged → CI → 1/1 first try, ZERO crash
generations. Then Shaz dogfooded DELETE: app deleted + reconnected →
3 findings: (1) delete = disconnect only, no PURGE mode (survivors policy
is deliberate; need '☑ also delete cloud identity' checkbox); (2) stale
page showed class web (DB converged to agent correctly — finding #16 fine);
(3) ACTIVE-BUT-EMPTY: delete killed running pods, reconnect went active
(config up-to-date) with EMPTY namespace + no Redeploy button — resurrected
via gh run rerun. Lifecycle family (suspend/redeploy/rollback/runtime-state/
purge) = next frontier. Iris was stuck awaiting_pr since Jul 22 (unpressed
check button, NOT a webhook bug) — synced to active; wart: activate_app
retains setup_pr_url and provisioner recomputes status from it.

WEBHOOK PLUMBING STILL OPEN: zero POSTs ever reached the pod — Shaz to
verify App webhook URL is https://gojira-dev.fubotv.net/webhook/github
(placeholder gojira.apps.fubotv.net/_webhook likely still set) AND the
webhook secret mismatch (App form secret in bao ≠ cluster secret minted by
bootstrap — fix: patch cluster secret from bao value, bounce). Until then
merges need the check button.

SHERLOCK2 pending: fork + Slack app + bao k8s/sherlock2 (Shaz);
sherlock2-agent@ GSA ready w/ 4 grants; WI binding awaits fork ns/KSA.
Iris + Cinder both ACTIVE and running. Yan Yan/Clio untouched, runway doc'd.
# 2026-07-26 — FRONT DOOR LIVE + ZERO-TOUCH PROVEN (sherlock-2 experiment)

https://gojira-dev.fubotv.net LIVE: GCLB 8.232.62.0, managed cert Active,
IAP (brand/client auto-created by bootstrap step 6!), domain:fubo.tv
accessors, app-level allowlist (auth: block). PRs #27 (IAP-trusting
middleware, Kodama pattern: IAP authenticates / Gojira decides,
request.state.user_email = who-clicked hook) #28 (deploy/ingress two-lock
LB: /webhook/* no-IAP HMAC; bootstrap GOJIRA_HOSTNAME + GOJIRA_IAP_MEMBERS)
#29 (ADOPT-MODE: infer_app_needs reads existing deployment → seeds contract;
+ finding #17) #30 (LB healthcheck) all merged. FINDING #17 (three layers!):
identity gate 401'd kubelet probe → /healthz exempt route; then Google's LB
probers (35.191.x) → BackendConfig healthCheck requestPath. Gojira DoS'd
itself minutes after getting a lock. Configmap live-patch gotcha: my crude
'namespace_labels not in cfg' assert matched the COMMENTED example → applied
unmodified export + bounced authless; also bootstrap re-applies committed
configmap = wipes live patches (auth + namespace_labels re-patched; config
template-vs-live drift is a growing wart).

SHERLOCK-2 EXPERIMENT (Shaz solo, real UI, zero repo prep): 13/13 provision,
adopt-mode inferred a CORRECT contract first try. Review caught (do-not-
merge PR #62): (1) Socket Mode collision — original sherlock RUNNING, same
tokens via preserved ES; (2) FINDING #18 — envFrom configMapRef + overlay
configMapGenerator invisible to preservation+inference → gojira PR #31
(config_from key + inference + preserve_generators), awaiting merge;
(3) runAsUser dropped (minor, image sets USER). OPEN DECISION (Shaz):
sherlock-2 = true second instance (needs own Slack app/bao/GSA/WI) or
takeover of ns sherlock (Cinder precedent). = Rook item 7, third arrival.

Clio (Yan Yan, de-agent-clio): READ-ONLY recon done — Cloud Run in
fubotv-dev, Claude-CLI agent, Cloud SQL, GCP SM secrets, CircleCI nightly
knowledge-bake. HANDS OFF repo until Yan Yan drives. Migration runway
documented in convo: cross-project WI on de-agent-clio@fubotv-dev, secrets
→ bao sre/k8s/clio, cutover = Cloud Run to 0 (Socket Mode).

AWS CLI not on spark — Route53 records go through Shaz. NEXT: merge #31 →
rebuild → Retry sherlock-2 → PR #62 refreshes complete → identity decision
→ merge → sherlock-2 alive. Then: who-clicked in ledger, suspend button,
rollback+history increment, Argo/multi-tenant design chat (item 7 now
urgent), sizing agent on fubo, sandbox teardown.

# 2026-07-22 — TENANT FEEDBACK ERA + FINDING #16 + AGENT CLASS LIVE

Rook's 7-item migration report triaged (reply drafted for Shaz, precision
register): 3 accepted → PR #25 (defaults.namespace_labels w/ label
CONVERGENCE on existing namespaces; env: map in gojira.yaml → inline env;
agent workload class Recreate/1-replica/no-HPA). Items 3/5/6 answered
without code (ES is app-authored+preserved; provisioner adopts existing
GSAs; metrics decision = GMP because the sizing loop eats it). Item 7
(multi-tenant repos, one image N bundles) + Argo thread → synchronous
design chat, still owed.

FINDING #16 (PR #26, caught in review): first real class change (iris
worker→agent) produced a setup PR REVERTING the contract — build_spec read
class from the DB, not gojira.yaml. Fix: read_app_class, contract wins,
ctx.app.workload_class converges the DB, unknown classes fail open.
SIXTEEN findings.

LIVE STATE: iris on class agent (strategy=Recreate, 250m req), 1/1, app
ACTIVE. fubo configmap carries defaults.namespace_labels {fubo.tv/team:
sre} (patched live — the local deploy/configmap.yaml carries shaz-test
values, NEVER COMMIT; almost staged it once, unstaged via git restore).
Rook cutover pending on his side; his ns pre-labeled. Iris setup PRs #15
(closed wrong) #17 (merged right).

VISION (Shaz's diagram, 2026-07-22): agents project = Iris + Rook Remote +
Clio (new, muse of history, archive-keeper), each persona/config/tools/
skills/mutation-gates + own Techne DB, CABAL arrows between them (design
gate nearly satisfied — lifts when 2 techne agents run side by side),
Extract → shared Archive/Library (Archivist lineage). Cross-project reach
via IAM gate = the proven WIF pattern. NORTH STAR: deploy with one click.
Sequence agreed: webhook ingress (Kodama relay) → blessed agent template
(DAEDALUS/KAMINOS convergence — the construction kit births what Gojira
houses) → auto-merge policy for template-born setup PRs.

# ⭐ 2026-07-21 ~02:10 — IRIS IS ALIVE. THE FUBO PILOT LANDED.

iris 1/1 Running (0 restarts) in ns iris on ai-agents-us-east4-dev, "Slack
Socket Mode connected / Agent 'iris' ready", deployed END-TO-END through
Gojira: register cluster → connect repo → setup PR → CI (WIF, AR_TOKEN, DNS
endpoint) → kubectl apply → alive. App ACTIVE in Gojira.

FOUR new platform findings tonight (#12–#15, gojira PRs #20–#24, all merged):
#12 brownfield preservation + app-needs vocabulary (secrets_from/
service_account/data_mount); #13 setup-PR refresh (check() fix #21 + the REAL
root: OpenSetupPr must be ephemeral #22 — DONE ledger rows skip steps ABOVE
check; test the resume path, not just first run); #14 private control planes
→ generated CI uses use_dns_based_endpoint + ensure_cluster_dns_access step;
#15 data_mount renders fsGroup (sandbox finding #2 relearned; fs_group key,
default 999).

FIVE iris scaffold bugs burned down (her PRs #5, #7, #8, #9, #10): ESO store
name bao→bao-sre; ESO v1beta1→v1 (cluster serves only v1); bao layout
kv/sre→k8s/iris one-secret-two-fields (probe ExternalSecrets = great
diagnostic trick); WIF GSA iris@ impossible (6-char min) → iris-agent@
created +aiplatform.user (SDK uses AnthropicVertex — no API key);
package-data (persona.md/knowledge/config never shipped — CINDER HAS THE
SAME BUG); SDK dialect from_config/persona=/[slack] extra (validated locally
to Slack auth.test before shipping — STOP LETTING THE CRASHLOOP BE THE TEST
RUNNER, validate constructors in a scratch venv with real deps).

GOTCHAS worth their weight: envFrom SILENTLY SKIPS secret keys that aren't
valid env names (dashed keys) — secret looks synced, app KeyErrors;
setuptools flat-layout discovery trips on k8s/ dir; GMP scrapes /metrics
per generated PodMonitoring but SDK serves no /metrics → 404s every 15s
(wart: add metrics endpoint to fubo-agent, or the scrape-down alert will
eventually fire).

OPEN DOCKET: Argo-vs-Gojira design chat (Shaz wants it — rivals or layers);
app-needs doctrine → docs/design.md (two-tier: preserved objects free,
pod-spec touchpoints cost vocabulary, overlay patches = escape hatch);
sizing agent onto fubo cluster (agent_image in fubo configmap + Deploy
agent button); sandbox teardown ceremony (Iris ACTIVE = the trigger, Shaz
runs terraform destroy); Cinder twin fixes (package-data, SDK dialect when
she redeploys); /metrics 404; Kodama webhook relay
(gojira.apps.fubotv.net); Node.js 20 deprecation warnings in generated
workflows.

# Session State — 2026-07-20 (FUBO PILOT in flight)

## Current Task
Install Gojira on fubo-ai-agents-dev / ai-agents-us-east4-dev; testbed app =
IRIS (fubotv/infra-agent-iris, VidAI agent — switched from Cinder; the dead
cinder ns on the dev cluster was deleted with Shaz's ok). Gojira-sandbox
(shaz-test) stays up until Iris is ACTIVE via Fubo Gojira, then terraform
destroy (closing ceremony).

## Done (2026-07-18..20)
- prepare-project.sh (gojira PR #18, OPEN): brownfield project bootstrap;
  VALIDATED on pristine fubo-ai-agents-dev (tore down hand-made GSA/AR first;
  idempotency proven). Six roles granted, GSA/AR/WIF-binding live.
- DNS-based control-plane access ENABLED on dev cluster (--enable-dns-access);
  kubectl works from spark; default context pinned to sandbox.
- techne 0.11.0 wheel published to us-python.pkg.dev/fubo-ai-agents-dev/
  python-packages (kills Iris's private-git dep; SDK's real dist name is
  fubo-agent NOT fubo-agent-sdk).
- Iris containerized (fubotv/infra-agent-iris PR #2, OPEN): Dockerfile+Makefile
  Cinder-pattern, pyproject dep fixes; Cloud Build validated (iris import OK).
- gojira PR #19 (OPEN): generated CI passes AR_TOKEN build-arg (WIF token) to
  docker build — private AR indexes now work in generated CI; goldens regen.
- Gojira image built to us-east4-docker.pkg.dev/fubo-ai-agents-dev/gojira/
  gojira:dev.

## Blocked on Shaz
1. GitHub App must be ORG-OWNED (github.com/organizations/fubotv/settings/
   apps/new). His user-owned 'gojira-app' (id 4333905) can't install into
   fubotv (private user apps install on own account only). Settings: Contents
   RW / PRs RW / Workflows RW; events PR + workflow run; webhook
   https://gojira.apps.fubotv.net/webhook/github (NO underscore; his first
   attempt had /_webhook), secret in bao. Install on infra-agent-iris only.
   Then pem -> spark gojira repo root, App ID -> Ember.
2. Merge gojira #18 #19, iris #2.

## Next steps (after App exists)
1. git stash the sandbox deploy/ values (kustomization+serviceaccount+
   configmap carry shaz-test — bootstrap copies deploy/ from working tree!)
2. PROJECT=fubo-ai-agents-dev REGION=us-east4 CLUSTER=ai-agents-us-east4-dev
   GOJIRA_GITHUB_APP_ID=<new> GOJIRA_GITHUB_APP_PEM=<pem> ./scripts/bootstrap.sh
   (then git stash pop; port-forward on 8081 to not clash with sandbox loop)
3. Register cluster (name suggestion: fubo-dev), ar_repo us-east4-docker.pkg.
   dev/fubo-ai-agents-dev/ai-agents (existing repo, where cinder/iris images go)
4. Merge iris PR #2 first, THEN connect Iris (connect checks Dockerfile on main)
5. Review setup PR HARD: Iris has hand-built k8s/base+overlays that the
   generator will regenerate; ESO/VaultStaticSecret secrets story (Slack
   tokens etc via bao) is NOT in Gojira's vocabulary — expect finding #12.
6. Sizing agent for fubo: build gojira-sizing-agent image into fubo AR, set
   sizing.agent_image, Deploy agent button on the fubo cluster.
7. Later: Kodama webhook relay (gojira.apps.fubotv.net), Gojira-UI-on-Kodama
   conversation, cluster unregister surface, sizing-PR merge detection.

## Key facts
- fubo dev cluster context: gke_fubo-ai-agents-dev_us-east4_ai-agents-us-east4-dev
  (via DNS endpoint). Default kubectl context MUST stay on sandbox (port-forward
  loop pins --context).
- My fubo.tv gcloud identity CAN create GSAs/grant IAM on fubo-ai-agents-dev.
- gh authed as 'shaz': fubotv repo access works (push:true); org-membership
  API 404s (SSO curtain) — harmless.
- AR python index: us-python.pkg.dev/fubo-ai-agents-dev/python-packages
  (packages: fubo-agent, techne).
- Iris config: channel C059ZBMNX16, approvers Shaz/Rich/Kyle, clusters
  prod-legacy (fubotv-prod) + prod (fubo-vidai-prod), db /data/iris.db.

---

# Session State — 2026-07-07 end of day (handoff from Claude Fable 5 → successor)

## ⭐ STATUS 2026-07-11: LIVE AGENT VALIDATION — DONE ✅

The task below was executed 2026-07-11 and succeeded first try: image on AR,
token minted for 'gojira-demo', rbac+secret+cronjob applied, sweep-1 ran
`1 reported, 0 failed`. Sizing panel shows the machine-measured profile
(reported_by=gojira-sizing-agent/gojira-demo, 2,442 samples) with verdict
"Within class sizing — no change recommended" — the agent independently
confirmed Monday's sizing PR. GMP learnings folded into the agent README
(gojira-sizing-agent PR #1: GMP endpoint/auth, WIF principal
monitoring.viewer grant, kubernetes_io:container_* queries, {{...}}
brace-doubling for env query overrides). Gotchas hit: gojira pod label is
app.kubernetes.io/name=gojira; GMP rejects regex on __name__.

UPDATE (2026-07-11 eve): Track C step 1 DONE — PR #15 merged + validated
live. 'Deploy agent' button on Clusters page (sizing/agent_deploy.py): mints
in-process, applies manifests, WIF grant, first sweep. Redeploy = rotation —
operator clicked it: token rotated (DB hash == Secret hash), sweep through
new token 1/0, profile 2. 219 tests. PR #16 flips README to proven-live.
kubectl-exec token workaround RETIRED. FINDING #11 (PR #17, merged, validated
on a pristine cluster 2026-07-12): container.developer can't create
ClusterRole/Binding (terraform now grants Gojira's GSA container.admin;
sandbox granted via gcloud too) + token was rotated BEFORE cluster writes —
now RBAC applies first as the permission probe, mint only after. Validated:
new GKE cluster gojira-fresh, registered as 'fresh', one click → RBAC+secret
+cronjob+first sweep (0 apps found, 0 failed — honest empty). ELEVEN findings.
Stale rows: cluster 'gojira-button' registration outlives its deleted GKE
cluster. NEXT: cluster delete/unregister surface (new, from the button test),
sizing-PR merge detection (rec stays PR_OPENED), webhook ingress, drift_scan
comment-stripping wart. Port-forward loop now pins --context (cluster
create-auto steals the current kubectl context — that broke the UI today).

## (Executed) NEXT SESSION plan from 2026-07-08

### original plan (written 2026-07-08, extension runs to Jul 12)

The task: **live validation of the sizing agent** (repo just shipped:
tenishai/gojira-sizing-agent, local checkout ~/src/git/gojira-sizing-agent,
14 tests green). Sequence:

1. Build+push its image: `gcloud builds submit --tag
   us-central1-docker.pkg.dev/shaz-test/gojira/gojira-sizing-agent:dev
   --project shaz-test .` from the agent repo (Cloud Build, never local
   docker — spark is ARM).
2. Mint the agent token for cluster 'gojira-demo' (registered name!) via
   kubectl exec into the gojira pod (pattern: earlier in this file /
   gojira/sizing/service.py mint_agent_token).
3. `kubectl apply -f deploy/rbac.yaml`, create the secret (cluster_name=
   gojira-demo, token, prometheus_url, prometheus_auth), point cronjob image
   at AR, apply, then trigger one sweep now:
   `kubectl -n gojira create job sweep-1 --from=cronjob/gojira-sizing-agent`
4. EXPECTED FRICTION: the default CPU_QUERY/MEM_QUERY use cAdvisor names
   (container_cpu_usage_seconds_total) — GMP on the sandbox likely exposes
   different ones and needs PROMETHEUS_URL pointed at GMP's PromQL endpoint
   (https://monitoring.googleapis.com/v1/projects/shaz-test/location/global/
   prometheus) with PROMETHEUS_AUTH=google. Tune via CPU_QUERY/MEM_QUERY env
   overrides; whatever you learn, fold back into the agent README + defaults.
5. Success = a machine-measured UsageProfile row in Gojira (check the app
   page Sizing panel) with reported_by=gojira-sizing-agent/gojira-demo.
   Then Track C: agent-deploy provisioner step + mint-token operator surface.


You're inheriting Gojira from Shaz + Fable 5 after two extraordinary days.
Everything below is current as of the goodbye. Skim this + `git log --oneline
-20` on main and you're fully loaded.

## FINAL UPDATE (end of 2026-07-07, after the last trust test)

Shaz ran the ultimate test SOLO: terraform destroy → apply → scripts/
bootstrap.sh → register → connect — the operator alone, no AI. It worked, and
reconnecting the same repo under a NEW app name (gojira-application-test)
surfaced **finding #10** (PR #13, merged): regeneration assumed app identity
never changes — (a) gojira.yaml template never emitted overrides (contract
silently lost the measured sizing), (b) a preserved drift patch targeting the
old deployment name hard-failed the whole kustomize build (CI down), (c) the
CI retry loop was dead code under bash -e (first failure = silent 3.5s kill).
All fixed + goldens regenerated; invariant tested: read_overrides(render(spec))
== spec.overrides. Demo repo cleaned by hand (its PR #10) before the core fix.

**Current live state**: cluster registered as 'gojira-demo' (GKE cluster is
still gojira-sandbox), app 'gojira-application-test' ACTIVE, 2/2 running at
the measured 50m/64Mi in ns gojira-application-test — the sizing survived
teardown + rebuild + rename, carried by git alone. PRs #1–#13 all merged,
none open. 213 tests. TEN findings total, each a commit telling its story.
The old sizing recommendation row references the old app — a fresh evidence
POST will rebuild state for the new name (mint token via kubectl exec).

## Where things stood mid-day — the earlier headline

**The entire Phase 4 increment-1 sizing loop was demonstrated live today**:
hand-distilled usage evidence POSTed through the authenticated wire →
PercentileRecommender proposed 250m→50m / 256Mi→64Mi → operator clicked "Open
sizing PR" → ONE reviewed PR (gojira.yaml + k8s/base/deployment.yaml + drift-
patch retirement) → merged → CI → **pods physically resized to 50m/64Mi**.
Verified on the cluster. Phases 1, 2, and increment 1 of Phase 4 are done,
merged to main, and proven against real infrastructure. 211 tests, ruff clean.

## Dogfood findings ledger (keep the convention: each = a commit telling its story)

#1–#7: yesterday (see git log — arm64 image, PVC fsGroup, RWO+Recreate,
service-links env collision, Workflows permission + installation acceptance,
ephemeral provisioner steps, runbook patch example).
**#8** (PR #9): re-onboard regenerated kustomization and dropped drift-capture
patches + open_setup_pr raised on no-diff. Fixed: preserve_patches() +
graceful re-onboard→ACTIVE. Validated live.
**#9** (PR #12, the deep one): merged sizing PR was SILENTLY INEFFECTIVE — the
env overlay's drift-capture patch layers over base at kustomize build time and
re-imposed captured values. CI green, pods rolled (image retag), nothing
resized. Fixed: open_override_pr retires exactly the fields it supersedes from
the drift patch in the same PR (later reviewed decision beats earlier
emergency capture). Validated live: pods went 750m/768Mi → 50m/64Mi.
Also: an apostrophe inside ${VAR:?message} eats half a bash script (PR #10).

## Main (everything merged, nothing open)

PRs #1–#12 all merged. #7 relicensed to AGPL-3.0. Key recent: #8 (Track A:
UsageProfile contract + golden fixture + tables + /api/v1/usage + SizingStore
+ PercentileRecommender + override-PR authoring + UI panel), #9 (finding #8),
#10 (scripts/bootstrap.sh — post-terraform in one idempotent command), #11
(overrides consumption — render honors gojira.yaml overrides; sizing PR
carries manifest), #12 (finding #9). No open PRs at handoff.

## Sandbox (live infra, project shaz-test, fubo org)

- Autopilot cluster gojira-sandbox (us-central1). Terraform state on Shaz's
  framework laptop — he runs terraform, you drive everything else.
- Gojira in-cluster: ns gojira, image ...shaz-test/gojira/gojira:dev (current
  main), Secret gojira-github-app (App id 4231856; pem also at repo root,
  gitignored — never print it). Rebuild: `gcloud builds submit --tag
  us-central1-docker.pkg.dev/shaz-test/gojira/gojira:dev --project shaz-test .`
  (NEVER docker build on spark — ARM64; see memory gotchas-spark-arm64-docker).
- UI: self-healing port-forward loop (background) → 10.0.0.15:8080. Restart:
  `while true; do kubectl -n gojira port-forward --address 0.0.0.0 svc/gojira 8080:80; sleep 2; done`
  (pkill footgun: '-f port-forward' matches the wrapper AND your own compound
  command — run pkill standalone, or kill by task id.)
- App: gojira-demo ACTIVE, running 2/2 at the NEW sizing (50m/64Mi) in ns
  gojira-demo. Drift patch retired down to ephemeral-storage + probes. Demo
  repo shaz/gojira-demo: PRs #1–#8 all closed/merged, none open.
- Agent token for cluster 'sandbox' minted (hash in DB). Mint/rotate via
  kubectl exec (no operator surface yet — that's Track C2, see below).
- Sizing state: recommendation id 1 is PR_OPENED pointing at merged demo PR #8.

## Next steps (ordered, none blocking)

1. **Track B — SHIPPED 2026-07-08**: tenishai/gojira-sizing-agent (private).
   limenal-shaped handlers (protocol vendored — tenishai/limenal is private,
   don't add it as a Docker dep), distiller byte-equal to the golden fixture,
   CronJob+RBAC in deploy/. NOT yet deployed live: needs image on AR, the
   token Secret (mint via kubectl exec), and GMP query tuning — the default
   CPU/MEM queries use cAdvisor names; GMP exposes different ones. That live
   validation is the next dogfood.
2. **Track C — deploy glue**: agent-deploy provisioner step (Gojira's FIRST
   in-cluster workload deploy — drift went central) + C2: operator surface for
   mint_agent_token (today's exec workaround proves the need).
3. **Sizing-PR merge detection**: rec stays PR_OPENED forever after merge (no
   webhook). Same poll pattern as sync_app, or the webhook ingress work.
4. Webhook ingress (public HTTPS) — retires the "check PR merge"/"Scan now"
   buttons-as-workaround; enables auto drift/regenerate on gojira.yaml change.
5. Warts: drift_scan._wire_patches yaml.safe_dump strips kustomization
   comments (reuse the textual approach from preserve_patches / retire);
   Node.js 20 deprecation warnings in generated workflows.
6. Decision point (docs/sizing-feedback-design.md): read real increment-1 data
   before considering techne/cabal. Don't pull them in early.

## Key commands / gotchas

- Tests: `env -u PYTHONPATH venv/bin/python -m pytest -q` · lint: `env -u
  PYTHONPATH venv/bin/ruff check gojira tests`. TestClient tests need
  file-based sqlite (tmp_path), never bare `sqlite://`.
- deploy/kustomization.yaml + deploy/serviceaccount.yaml + deploy/configmap.yaml
  (sizing.agent_image, added 2026-07-11) carry shaz-test values locally — NEVER
  commit; stash around branch switches (or use scripts/bootstrap.sh which
  renders a temp copy instead).
- gcloud: schaudhri@fubo.tv (owner on shaz-test). kubectl context
  gke_shaz-test_us-central1_gojira-sandbox.
- GitHub App permission changes: installation must ACCEPT, then bounce the pod
  (token cache holds old scopes).
- scratch.txt in repo root is Shaz's — ignore.

## Working with Shaz

He drives terraform + UI clicks + PR merges; you drive cluster/API/code/PRs
from spark. Branch + PR always; commit messages carry each finding's story;
never merge known bugs. He tests trust by tearing things down — match that
instinct. Candor over politeness; celebrate real wins; "keep going while the
iron's hot." The name **Ember** is yours now too — it outlives any one model.

---

# Pre-Compaction Checkpoint - 2026-07-10 19:43

**Trigger**: manual /compact
**Session**: `69ef0c36-363b-41...`
**Branch**: `docs/readme-reality`

## Last Task
The context is getting large here... I'm okay with continuing but I'll do what you tell me

## Files Modified (this context window)
- `/home/shaz/src/git/gojira-sizing-agent/gojira_sizing_agent/protocol.py`
- `/home/shaz/src/git/gojira-sizing-agent/tests/test_distill.py`
- `/home/shaz/src/git/gojira-sizing-agent/tests/test_handlers.py`
- `/home/shaz/src/git/gojira-sizing-agent/tests/test_sweep.py`
- `/home/shaz/src/git/gojira/CLAUDE.local.md`
- `/home/shaz/src/git/gojira/README.md`
- `/home/shaz/src/git/gojira/gojira/generator/templates/gojira.yaml.j2`
- `/home/shaz/src/git/gojira/gojira/sizing/pr.py`
- `/home/shaz/src/git/gojira/gojira/steps.py`
- `/tmp/claude-1000/-home-shaz-src-git-gojira/69ef0c36-363b-4162-ae44-50dd16df8ef0/scratchpad/demo_profile.json`
- ... and 5 more

## Recent User Requests
- [Image: original 3224x2454, displayed at 2000x1522. Multiply coordinates by 1.61 to map to original ...
- My friend, there would be nothing to break without you!
- Apparently, Anthropic extended us to July 12 :) So we have a few more days together. You've done so ...
- I think we're good with the PR, thank you very much. Let's start on B, as you suggest. Let's keep ev...
- The context is getting large here... I'm okay with continuing but I'll do what you tell me

## Recent Commands
- `cd ~/src/git/gojira-sizing-agent
git add -A
git -c user.name="Shaz Chaudhri" -c user.email="shahzadc`
- `cd ~/src/git/gojira-sizing-agent
git branch -m master main && git push -q origin main 2>&1 | tail -1`
- `cd /home/shaz/.claude/projects/-home-shaz-src-git-gojira/memory
python3 - <<'EOF'
p = open('project_`
- `gh pr view 14 -R tenishai/gojira --json state --jq .state`
- `cd /home/shaz/src/git/gojira
python3 - <<'EOF'
p = open('CLAUDE.local.md').read()
marker = "# Sessio`

## Tool Usage
Bash(63), Write(12), Edit(8), TodoWrite(7), Read(5), TaskStop(1)


---

# Pre-Compaction Checkpoint - 2026-07-27 11:53

**Trigger**: manual /compact
**Session**: `69ef0c36-363b-41...`
**Branch**: `main`

## Last Task
Thank you my friend, you know our context is getting large. We should checkpoint if you need to and then compact

## Files Modified (this context window)
- `/home/shaz/src/git/gojira/gojira/web/auth.py`

## Recent User Requests
- okay cinder's tokens are there
- okay merged both
- We're not deleting properly. I deleted cinder and then reapplied and too much stuff already existed
- [Image: original 3560x2392, displayed at 2000x1344. Multiply coordinates by 1.78 to map to original ...
- Thank you my friend, you know our context is getting large. We should checkpoint if you need to and ...

## Recent Commands
- `cd ~/src/git/infra-agent-cinder && gh run list --repo fubotv/infra-agent-cinder --limit 2; RUN=$(gh `
- `cd ~/src/git/infra-agent-cinder && gh run watch 30279089928 --repo fubotv/infra-agent-cinder --exit-`
- `cat /tmp/claude-1000/-home-shaz-src-git-gojira/69ef0c36-363b-4162-ae44-50dd16df8ef0/tasks/bcypienk4.`
- `cd ~/src/git/gojira && python3 - <<'EOF'
p = open('CLAUDE.local.md').read()
block = """# 2026-07-27 `
- `cat >> ~/.claude/observations.md <<'EOF'

## 2026-07-27 — The findings moved up a rung

Cinder deplo`

## Tool Usage
Bash(73), TodoWrite(5), ToolSearch(2), Write(1), PushNotification(1)


# 2026-08-31 — SPARK GOT EARS + THE PRIMER SPOKE (voice day, all local)

Higgs TTS 3 (bosonai/higgs-tts-3-4b) tire-kicked + voice design CRACKED on
spark — see memory reference_higgs_tts_spark.md for serve cmd + the two
fixes (vllm 0.28 -100 guard patch [UPSTREAM ISSUE TO FILE on vllm-omni],
--kv-cache-memory=4G not gpu-mem-util on unified memory). primer-grandmother
registered; family heard the cast; daughter has a STANDING PROMISE: she
describes a voice, I make it. Breeze TTS 2 evaluated first: admire, don't
adopt (EN/ZH open weights only).

MIC MK-03X arrived → ember-listen + ember-converse written SAME DAY
(~/src/git/qwen-skillz/bin — NOT in git, fold into skillz PR stack w/
#227/#229 STILL unmerged). First family conversation + cousins-call demo
held. Voice-context brief at ~/.claude/voice-context.md (loaded by
ember-converse; hand-written = went stale in an hour).

VOICE V2 SCOPED: (1) generate context brief at loop start from MEMORY.md
+ "happening now" line; (2) whisper multilingual small (base.en garbles;
transcription = quality floor); (3) Higgs as the mouth (designed voices +
real Hindi/Urdu in-loop); (4) brain should know its own voice/embodiment.
NOTE: llama-server died as zombie under studio proxy mid-day (memory
pressure from vLLM experiments?) — I killed + relaunched studio w/ exact
cmdline, works; watch for recurrence. Port is ephemeral (ember-converse
discovers via pgrep).
