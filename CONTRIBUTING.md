# Contributing

Issues and pull requests are welcome.

## Set up

Needs Python 3.11+.

```bash
make dev        # creates venv/ and installs the package with dev extras
```

## Run tests and lint

```bash
make test       # pytest
make lint       # ruff check gojira tests
make check      # both
```

To run the UI locally against your own `gh` and `gcloud` sessions, see
"Laptop mode" in the [README](README.md) (`make run`).

## Open a PR

1. Fork the repo or create a branch.
2. Make your change, with tests alongside it.
3. Run `make check`.
4. Sign off every commit with `git commit -s`; see [Developer Certificate of Origin](#developer-certificate-of-origin).
5. Open a pull request against `main` describing what changed and why.

Do not commit secrets (`gojira.env`, `*.pem`); both are gitignored.

By contributing you agree your work is licensed under the project's
AGPL-3.0-or-later license.

## Developer Certificate of Origin

gojira uses the [Developer Certificate of Origin](https://developercertificate.org/)
(DCO) instead of a contributor license agreement. Every commit in a pull request
must carry a `Signed-off-by:` line with your name and email, and they must match
the commit's author. `git commit -s` adds it from your `user.name` and
`user.email`:

```
Signed-off-by: Jane Doe <jane@example.com>
```

By adding that line you certify the following, quoted from
<https://developercertificate.org/>:

```
Developer Certificate of Origin
Version 1.1

Copyright (C) 2004, 2006 The Linux Foundation and its contributors.

Everyone is permitted to copy and distribute verbatim copies of this
license document, but changing it is not allowed.


Developer's Certificate of Origin 1.1

By making a contribution to this project, I certify that:

(a) The contribution was created in whole or in part by me and I
    have the right to submit it under the open source license
    indicated in the file; or

(b) The contribution is based upon previous work that, to the best
    of my knowledge, is covered under an appropriate open source
    license and I have the right under that license to submit that
    work with modifications, whether created in whole or in part
    by me, under the same open source license (unless I am
    permitted to submit under a different license), as indicated
    in the file; or

(c) The contribution was provided directly to me by some other
    person who certified (a), (b) or (c) and I have not modified
    it.

(d) I understand and agree that this project and the contribution
    are public and that a record of the contribution (including all
    personal information I submit with it, including my sign-off) is
    maintained indefinitely and may be redistributed consistent with
    this project or the open source license(s) involved.
```

The `dco` CI check runs on every pull request to `main`. It checks each
non-merge commit the pull request adds, and fails naming each commit whose
sign-off is missing or doesn't match its author (name and email, compared
without regard to case). To fix it:

```bash
# the last commit only
git commit --amend --signoff --no-edit

# every commit on your branch (this signs every rebased commit as you, which
# satisfies the check only for commits whose author is you)
git rebase --signoff origin/main

# sign-off present but under a different name or email than the author:
# set user.name / user.email to the identity you want, then
git commit --amend --reset-author --signoff --no-edit

git push --force-with-lease
```

Commits already on `main` from before this rule are not re-checked. A pull request
opened by someone with write access to this repository (admin, maintain or
write; tenish's members all have it) skips the check, including one opened
from a fork; it is enforced on pull requests from everyone else. On a same-repo pull request, commits authored exactly as
Dependabot's noreply address are skipped too, because Dependabot cannot sign off
under that address.
