import pytest
from sim.config import SimConfig
from sim.main import build_simulation
from sim.population.agents import PopulationManager
from sim.population.calibration import calibrate_from_csv
from sim.population.behaviour import PopulationBehaviourModel
import os

def test_full_simulation_loop(monkeypatch):
    from sim.chrono.store import PostgresChronoDAG
    
    class MockChronoDAG:
        def __init__(self, *args, **kwargs):
            pass
        def save_event(self, event):
            pass
        def diff(self, *args, **kwargs):
            return None
        def fork(self, *args, **kwargs):
            return None

    monkeypatch.setattr("sim.main.PostgresChronoDAG", MockChronoDAG)

    config = SimConfig(seed=42, num_users=50, sim_duration_days=1, db_url="postgresql://mock:5432")
    engine, gateway, dag = build_simulation(config)
    
    data_dir = os.path.join(os.path.dirname(__file__), "..", "..", "data", "paysim")
    params = calibrate_from_csv(data_dir)
    
    behaviour_model = PopulationBehaviourModel(params, engine._rng)
    population = PopulationManager(behaviour_model, engine._rng)
    population.create_population(num_users=50, num_merchants=5)
    population.start_agent_loops(engine)
    
    # Run simulation for just a small amount of time to verify it steps
    engine._env.run(until=0.1 * 24 * 3600 * 1e9)
    assert engine._env.step_count > 0
