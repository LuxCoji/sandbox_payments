"""Manual smoke test: one real red-team session against whichever providers
have a configured API key in .env (build_router() skips unconfigured ones —
see agents/redteam/llm_router.py). Not part of `make test` — this makes real
network calls to real LLM providers and needs a real Postgres/FINSIM_DB_URL.

Usage:
    uv run python scripts/red_team_smoke_test.py

Skips cleanly (exit 0) if no provider key or FINSIM_DB_URL is configured, so
it never breaks CI/environments without credentials.
"""
from __future__ import annotations

import os
import sys
import time

from dotenv import load_dotenv

from agents.redteam.config import RedTeamConfig, load_provider_deployments
from sim.config import SimConfig
from sim.observability import get_logger, setup_tracing

logger = get_logger("finsim.redteam.smoketest")

_KNOWN_KEY_ENVS = ("GROQ_API_KEY_1", "OPENROUTER_API_KEY_1", "GEMINI_API_KEY_PROJECT_1", "NVIDIA_API_KEY_1")


def main() -> None:
    load_dotenv()

    if not os.environ.get("FINSIM_DB_URL"):
        print("SKIP: FINSIM_DB_URL not set — smoke test needs a real Postgres/Supabase connection.")
        sys.exit(0)

    configured = [k for k in _KNOWN_KEY_ENVS if os.environ.get(k)]
    if not configured:
        print(f"SKIP: none of {_KNOWN_KEY_ENVS} are set in .env — nothing to test against.")
        sys.exit(0)

    print(f"Running smoke test against configured providers: {configured}")
    setup_tracing("finsim.redteam.smoketest")

    from agents.redteam.harness import run_session

    # Deterministic event_ids (see CLAUDE.md) are derived from branch_id+seed
    # -- a fixed seed=42 against the real "main" branch collides with events
    # from any prior run (UniqueViolation on event_id). This script is a
    # throwaway wiring check re-run repeatedly, not part of the determinism
    # contract, so vary the seed per invocation rather than fixing it.
    sim_config = SimConfig(seed=int(time.time()) % 1_000_000, num_users=5, num_merchants=1)
    redteam_config = RedTeamConfig(warmup_hours=0.1, session_max_steps=2)

    # Fail fast with a clear message if providers.yaml itself is malformed,
    # before spending a warmup run on a config that was never going to work.
    load_provider_deployments(redteam_config)

    result = run_session(sim_config, redteam_config, from_genesis=True, session_id="smoketest")

    print(f"Session complete: branch={result.branch_id} steps={result.steps_taken} committed={result.committed}")
    for entry in result.step_log:
        print(f"  - {entry['tool_name']}: success={entry['success']} error={entry['error_code']}")

    if result.steps_taken == 0:
        print("FAIL: session took zero steps")
        sys.exit(1)
    print("OK")


if __name__ == "__main__":
    main()
