.PHONY: venv install test lint build run clean

VENV := venv
# env -u PYTHONPATH: a globally-set PYTHONPATH leaks foreign packages into
# venvs and makes pip skip installing "already satisfied" deps. Strip it.
PY := env -u PYTHONPATH $(VENV)/bin/python

venv:
	python3 -m venv $(VENV)

install: venv
	$(PY) -m pip install -q -e ".[dev]"

test:
	$(PY) -m pytest -q

lint:
	env -u PYTHONPATH $(VENV)/bin/ruff check gojira tests

build:
	docker build -t gojira:dev .

run:
	$(PY) -m gojira.web

clean:
	rm -rf $(VENV) *.egg-info .pytest_cache .ruff_cache
