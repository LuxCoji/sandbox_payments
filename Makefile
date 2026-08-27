.PHONY: test test-contract test-integration lint typecheck calibrate run regress install api frontend frontend-install

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
	uv sync --extra dev

# Web UI: FastAPI backend (in-process live simulation + ChronoDAG) on :8000,
# and the Vite/React frontend on :5173 (proxies /api -> :8000). Run each in
# its own terminal: `make api` then `make frontend`.
api:
	uv run uvicorn api.main:app --reload --port 8000

frontend-install:
	cd frontend && npm install

frontend:
	cd frontend && npm run dev
