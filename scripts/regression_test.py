import json
import sys
from pathlib import Path

from sim.chrono.store import PostgresChronoDAG
from sim.observability import get_logger, setup_tracing

logger = get_logger("finsim.regression")

BASELINE_FILE = Path(__file__).parent.parent / "baselines" / "hashes.json"


def load_baselines() -> dict[str, str]:
    if not BASELINE_FILE.exists():
        return {}
    with open(BASELINE_FILE, "r") as f:
        return json.load(f)


def save_baselines(baselines: dict[str, str]) -> None:
    BASELINE_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(BASELINE_FILE, "w") as f:
        json.dump(baselines, f, indent=2)


def run_regression() -> None:
    """Run regression tests against known deterministic seeds to verify hash invariance."""
    setup_tracing("finsim.regression")
    logger.info("Starting deterministic regression test harness")
    
    baselines = load_baselines()
    failed = 0
    seeds_to_test = [42, 100, 999]
    db_url = "postgresql://postgres:postgres@localhost:5432/finsim"
    
    from sim.config import SimConfig
    from sim.main import build_simulation
    from sim.population.agents import PopulationManager
    from sim.population.calibration import calibrate_from_csv
    from sim.population.behaviour import PopulationBehaviourModel
    import os

    data_dir = os.path.join(os.path.dirname(__file__), "..", "data", "paysim")
    try:
        params = calibrate_from_csv(data_dir)
    except FileNotFoundError:
        # Fallback if csvs not generated
        from sim.population.interfaces import CalibratedParams
        params = CalibratedParams({}, (), {}, {}, {})
        
    for seed in seeds_to_test:
        logger.info("Testing seed", seed=seed)
        
        # 1. Boot simulation
        config = SimConfig(seed=seed, num_users=100, sim_duration_days=1, db_url=db_url)
        engine, gateway, dag = build_simulation(config)
        
        behaviour_model = PopulationBehaviourModel(params, engine._rng)
        population = PopulationManager(behaviour_model, engine._rng)
        population.create_population(num_users=100, num_merchants=10, engine=engine)
        population.start_agent_loops(engine)
        
        # Run simulation for a fixed number of events or fixed time (e.g., 24 hours)
        engine._env.run(until=24 * 3600 * 1e9)
        current_hash = engine.get_state_hash()
        
        if str(seed) not in baselines:
            logger.info("New baseline recorded", seed=seed, hash=current_hash)
            baselines[str(seed)] = current_hash
        else:
            expected = baselines[str(seed)]
            if current_hash != expected:
                logger.error("DETERMINISM FAILURE", seed=seed, expected=expected, got=current_hash)
                failed += 1
            else:
                logger.info("Seed passed", seed=seed)
                
    save_baselines(baselines)
    
    if failed > 0:
        logger.error("Regression suite failed", failed_count=failed)
        sys.exit(1)
        
    logger.info("Regression suite complete.")


if __name__ == "__main__":
    run_regression()
