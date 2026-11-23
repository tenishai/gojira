# Security policy

## Reporting a vulnerability

Please don't open a public issue for a security problem.

Report it privately to **security@tenish.ai**.

If this repository's **Security** tab shows a **Report a vulnerability**
button, you can use that instead. GitHub offers it only on public
repositories where it has been turned on.

Include what you can of:

- what the problem is and what an attacker could do with it;
- the version, commit, or tag you tested;
- steps to reproduce, or a proof of concept;
- any configuration it depends on.

You don't need a complete write-up to report. A clear description of the
problem is enough to start.

## What happens next

- We acknowledge your report within **14 days**.
- We tell you whether we've confirmed the problem as soon as we know.
- We fix confirmed problems on `main`, and tell you before the fix is public.
- We credit you in the fix's commit or pull request unless you'd rather we didn't.

We ask that you give us a reasonable chance to fix a problem before disclosing
it publicly, and that you don't access or change data that isn't yours while
testing.

## Supported versions

gojira has tagged releases (`vX.Y.Z` on `main`, listed under Releases). Security
fixes land on `main` and ship in the next release; only the latest release is
patched. Run the latest release, or the current `main`.

## Scope

gojira connects GitHub repositories to GKE: it provisions cloud resources,
opens setup pull requests, and deploys on push. In scope:

- access: reaching gojira's web UI or API without the identity or token it is
  configured to require;
- credentials: the GitHub App key or secret, installation tokens, or cloud
  credentials leaking through a log, an error, a response, or a generated file;
- isolation: one connected repository or tenant reading, changing, or deploying
  into another's resources;
- generated output: repository content (a contract, manifest, or `gojira.yaml`)
  that makes gojira generate a workflow or manifest that runs code or grants
  access it should not.

**Out of scope:** vulnerabilities in third-party dependencies (please report
those upstream, and tell us if we ship an affected version), and problems that
require an attacker to already control the cluster, the cloud project, the
GitHub App, or the account gojira runs under.
