"""CLI entrypoint for one red-team session.

Usage:
    uv run python scripts/red_team_run.py --checkpoint <id> [--seed 42]
    uv run python scripts/red_team_run.py --from-genesis [--seed 42] [--users 200]

Requires `make install-redteam` (litellm/langgraph) and provider API keys in
.env — see agents/redteam/providers.yaml.
"""
from __future__ import annotations

import argparse

from dotenv import load_dotenv

from agents.redteam.config import RedTeamConfig
from agents.redteam.harness import run_session
from sim.config import SimConfig
from sim.observability import get_logger, setup_tracing

logger = get_logger("finsim.redteam.cli")


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run one red-team agent session against FinSim")
    parser.add_argument("--checkpoint", type=str, default=None, help="Warm-up checkpoint id to fork from")
    parser.add_argument("--from-genesis", action="store_true", help="Run a fresh warmup instead of forking")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--users", type=int, default=200, help="Population size for --from-genesis warmup")
    parser.add_argument("--db-url", type=str, default=None, help="Overrides FINSIM_DB_URL")
    parser.add_argument(
        "--no-risk", action="store_true",
        help="Run with fraud detection off. The default is ON here, unlike "
             "everywhere else - an attack run against a system with no "
             "controls measures nothing.")
    return parser


def main() -> None:
    load_dotenv()
    args = _build_arg_parser().parse_args()

    if not args.from_genesis and not args.checkpoint:
        raise SystemExit("Pass --checkpoint <id> or --from-genesis")

    setup_tracing("finsim.redteam")

    # Risk defaults ON for a red-team run and OFF everywhere else, and the
    # asymmetry is deliberate. Off is right as a global default: it keeps the
    # engine byte-identical to what every replay and determinism test expects.
    # But an attack session against a system with the controls switched off is
    # not a test of anything - it measures an undefended simulator, and every
    # "nothing flagged it" finding it produces is true and meaningless.
    sim_config = SimConfig(seed=args.seed, num_users=args.users,
                           db_url=args.db_url or SimConfig().db_url,
                           enable_risk=not args.no_risk)
    redteam_config = RedTeamConfig()
    logger.info("fraud detection", enabled=sim_config.enable_risk)

    result = run_session(
        sim_config, redteam_config,
        warmup_checkpoint_id=args.checkpoint, from_genesis=args.from_genesis,
    )

    logger.info(
        "Session complete", session_id=result.session_id, branch_id=result.branch_id,
        steps_taken=result.steps_taken, committed=result.committed,
    )


if __name__ == "__main__":
    main()
