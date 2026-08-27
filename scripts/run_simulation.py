import argparse
import sys

from sim.chrono.store import PostgresChronoDAG
from sim.observability import get_logger, setup_tracing, start_metrics_server

logger = get_logger("finsim.cli")


def run_seed(args: argparse.Namespace) -> None:
    logger.info("Initializing simulation...", seed=args.seed, users=args.users)
    
    # Initialize the DAG and ensure schema is ready
    dag = PostgresChronoDAG(args.db_url)
    
    # [!] Placeholder: Engine instantiation will go here once Person 1 writes `sim.main`.
    # engine = WorldEngine(dag, seed=args.seed)
    # population = PopulationManager(engine, args.users)
    # engine.run(until=args.duration_hours * 3600 * 1e9)
    
    logger.info("Simulation finished (placeholder: waiting on WorldEngine implementation)")


def fork_branch(args: argparse.Namespace) -> None:
    logger.info("Forking branch...", parent_checkpoint=args.checkpoint, new_branch=args.branch)
    
    dag = PostgresChronoDAG(args.db_url)
    branch = dag.fork(checkpoint_id=args.checkpoint, branch_id=args.branch)
    
    logger.info("Branch forked successfully", seed_offset=branch.seed_offset)


def diff_branches(args: argparse.Namespace) -> None:
    logger.info("Diffing branches...", branch_a=args.branch_a, branch_b=args.branch_b, at_event=args.event)
    
    dag = PostgresChronoDAG(args.db_url)
    try:
        diff = dag.diff(args.branch_a, args.branch_b, args.event)
        logger.info(
            "Diff computed", 
            added=len(diff.entities_added), 
            removed=len(diff.entities_removed),
            modified=len(diff.entities_modified),
            unique_events_a=diff.events_only_in_a,
            unique_events_b=diff.events_only_in_b
        )
    except Exception as e:
        logger.error("Failed to compute diff", error=str(e))
        sys.exit(1)


def main() -> None:
    parser = argparse.ArgumentParser(description="FinSim CLI Runner (Adversarial Sandbox)")
    parser.add_argument("--db-url", type=str, default="postgresql://postgres:postgres@localhost:5432/finsim", help="PostgreSQL connection URL")
    parser.add_argument("--metrics-port", type=int, default=8000, help="Port for Prometheus metrics")
    
    subparsers = parser.add_subparsers(dest="command", required=True)
    
    # Command: run-seed
    p_run = subparsers.add_parser("run-seed", help="Run a new simulation from a deterministic seed")
    p_run.add_argument("--seed", type=int, required=True, help="Deterministic RNG seed")
    p_run.add_argument("--users", type=int, default=1000, help="Number of users to generate")
    p_run.add_argument("--duration-hours", type=int, default=24, help="Simulation duration in hours")
    
    # Command: fork
    p_fork = subparsers.add_parser("fork", help="Fork a new alternate timeline branch from a checkpoint")
    p_fork.add_argument("--checkpoint", type=str, required=True, help="Checkpoint ID to fork from")
    p_fork.add_argument("--branch", type=str, required=True, help="Name of the new branch")
    
    # Command: diff
    p_diff = subparsers.add_parser("diff", help="Compare states between two branches at a specific event")
    p_diff.add_argument("--branch-a", type=str, required=True)
    p_diff.add_argument("--branch-b", type=str, required=True)
    p_diff.add_argument("--event", type=int, required=True, help="Event sequence number to diff at")
    
    args = parser.parse_args()
    
    # Boot Observability (Traces, Metrics, Logs)
    setup_tracing("finsim.runner")
    start_metrics_server(args.metrics_port)
    logger.info("Observability started", metrics_port=args.metrics_port)
    
    if args.command == "run-seed":
        run_seed(args)
    elif args.command == "fork":
        fork_branch(args)
    elif args.command == "diff":
        diff_branches(args)


if __name__ == "__main__":
    main()
