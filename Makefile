.PHONY: up down test test-contract test-integration lint typecheck calibrate run regress install

up:
	docker compose up -d

down:
	docker compose down

test:
	uv run pytest -xvs

test-contract:
	uv run pytest -xvs -k contract_test

test-integration:
	uv run pytest -xvs tests/integration/

lint:
	uv run ruff check .
	uv run lint-imports

typecheck:
	uv run mypy sim/

calibrate:
	uv run python scripts/calibrate.py

run:
	uv run python scripts/run_simulation.py

regress:
	uv run python scripts/regression_test.py

install:
	uv sync --dev
