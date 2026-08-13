.PHONY: install lint fmt test evals security gate docker compose-check clean

PYTHON ?= python3

install:
	$(PYTHON) -m pip install -r requirements-dev.txt

lint:
	$(PYTHON) -m ruff check src tests
	$(PYTHON) -m ruff format --check src tests
	$(PYTHON) -m mypy src

fmt:
	$(PYTHON) -m ruff check --fix src tests
	$(PYTHON) -m ruff format src tests

test:
	$(PYTHON) -m pytest --cov=src --cov-report=xml --cov-report=term-missing \
		--cov-fail-under=80 -q

evals:
	$(PYTHON) -m pytest tests/evals -q

security:
	$(PYTHON) -m pip_audit --requirement requirements.lock

gate: lint test evals security

docker:
	docker build -t financial-research-agent:local .
	docker run --rm --entrypoint id financial-research-agent:local -u | grep -vx 0

compose-check:
	docker compose config --quiet

clean:
	rm -rf .coverage coverage.xml htmlcov .pytest_cache .mypy_cache .ruff_cache
