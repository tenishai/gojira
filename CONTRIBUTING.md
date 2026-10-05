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
4. Open a pull request against `main` describing what changed and why.

Do not commit secrets (`gojira.env`, `*.pem`); both are gitignored.

By contributing you agree your work is licensed under the project's
AGPL-3.0-or-later license.
