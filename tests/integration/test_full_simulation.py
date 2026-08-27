"""Multi-agent full-simulation smoke test at representative scale.

See test_determinism.py for why duration is kept short (superlinear event
growth was observed with duration at this population size).
"""
from __future__ import annotations

import os

from sim.chrono.tests._fake_dag import InMemoryChronoDAG
from sim.config import SimConfig
from sim.main import build_simulation
from sim.population.agents import PopulationManager
from sim.population.behaviour import PopulationBehaviourModel
from sim.population.calibration import calibrate_from_csv

NUM_USERS = 500
NUM_MERCHANTS = 50
DURATION_HOURS = 3


def test_full_simulation_loop(monkeypatch) -> None:
    monkeypatch.setattr("sim.main.PostgresChronoDAG", lambda db_url=None: InMemoryChronoDAG())

    config = SimConfig(seed=42, num_users=NUM_USERS, sim_duration_days=1, db_url="postgresql://mock:5432")
    engine, gateway, dag = build_simulation(config)

    data_dir = os.path.join(os.path.dirname(__file__), "..", "..", "data", "paysim")
    params = calibrate_from_csv(data_dir)

    behaviour_model = PopulationBehaviourModel(params, engine._rng)
    population = PopulationManager(behaviour_model, engine._rng)
    population.create_population(num_users=NUM_USERS, num_merchants=NUM_MERCHANTS, engine=engine)
    population.start_agent_loops(engine)

    engine._env.run(until=DURATION_HOURS * 3600 * 1e9)

    assert engine._env.step_count > 0
    # Every scheduled agent step that resulted in a command must have
    # persisted its events through the real ChronoDAG wiring (Phase 1).
    assert len(dag._events) > 0
    assert all(e.branch_id == "main" for e in dag._events)
    # get_state_hash must be computable post-run without raising.
    assert engine.get_state_hash()
