# FinSim, as one container: the dashboard built to static files and served by
# the same FastAPI process that serves the API.
#
# One image rather than two services because the frontend calls `/api/...` on
# its own origin. Split across two services that becomes a cross-origin setup
# needing CORS rules and a second URL to keep in step, for no benefit - the two
# halves are always deployed together anyway.

# ── Stage 1: build the dashboard ──────────────────────────────────────────
FROM node:20-slim AS frontend

WORKDIR /build
# package files first, so a source-only change does not reinstall node_modules.
COPY frontend/package*.json ./
RUN npm ci --no-audit --no-fund

COPY frontend/ ./
RUN npm run build


# ── Stage 2: the service ──────────────────────────────────────────────────
FROM python:3.12-slim

# libgomp is xgboost's OpenMP runtime. The wheel links against it and the slim
# image does not ship it, so the wire model import fails at load time with a
# missing-library error rather than anything about xgboost.
RUN apt-get update \
    && apt-get install -y --no-install-recommends libgomp1 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# CPU-only torch first, so the resolver below does not fetch the CUDA wheel -
# about two gigabytes of runtime Cloud Run has no GPU to use, and enough to push
# the image past what a cold start should carry.
RUN pip install --no-cache-dir torch \
        --index-url https://download.pytorch.org/whl/cpu

# The rest from requirements-deploy.txt, which is generated from pyproject.toml
# rather than written by hand.
#
# It was written by hand, and the hand-written list missed opentelemetry -
# nothing in `risk` imports it, but `sim.observability` does at module scope. So
# the image built, nothing tested it, and the container died on import with a
# stack trace about uvicorn. A second copy of a dependency list is a copy that
# drifts; `make deploy-reqs` regenerates this one.
COPY requirements-deploy.txt ./
RUN pip install --no-cache-dir -r requirements-deploy.txt

COPY sim/ ./sim/
COPY risk/ ./risk/
COPY api/ ./api/
COPY models/ ./models/
COPY scripts/ ./scripts/

# The PaySim calibration CSVs are gitignored, so they are not in the build
# context - the image shipped with an empty data/ and the population produced
# no intents at all. The simulation created its 68 accounts, the event log
# showed nothing but DailyCountersReset, and the Fraud tab reported zero
# assessed while looking perfectly healthy.
#
# `download_data.py` generates them from parameters in the script itself, with
# no network call, so building them here is deterministic and needs nothing
# fetched.
RUN python scripts/download_data.py     && test -s data/paysim/clientsProfiles.csv

COPY --from=frontend /build/dist ./static

# The rails are on here, unlike the library default. A deployment with fraud
# detection switched off would be a payment simulator with a Fraud tab that
# reports nothing, which is worse than not deploying it.
ENV FINSIM_ENABLE_RISK=1 \
    PYTHONUNBUFFERED=1

# Cloud Run supplies $PORT and it is not always 8080. One worker, deliberately:
# the simulation is a module-level object driven by an asyncio task, so a second
# worker is a second independent simulation and the dashboard would show
# different numbers depending on which one answered.
CMD exec uvicorn api.main:app --host 0.0.0.0 --port ${PORT:-8080} --workers 1
