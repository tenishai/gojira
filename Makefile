.PHONY: venv install test lint build run clean

VENV := venv
PY := $(VENV)/bin/python

venv:
	python3 -m venv $(VENV)

install: venv
	$(PY) -m pip install -q -e ".[dev]"

test:
	$(PY) -m pytest -q

lint:
	$(VENV)/bin/ruff check gojira tests

build:
	docker build -t gojira:dev .

run:
	$(PY) -m uvicorn gojira.app:app --reload --port 8080

clean:
	rm -rf $(VENV) *.egg-info .pytest_cache .ruff_cache
