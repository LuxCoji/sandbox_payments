import pytest
from sim.config import SimConfig
from sim.main import build_simulation
from sim.population.agents import PopulationManager
from sim.population.calibration import calibrate_from_csv
from sim.population.behaviour import PopulationBehaviourModel
import os

def run_sim_for_hash(seed, monkeypatch):
    from sim.chrono.store import PostgresChronoDAG
    
    class MockChronoDAG:
        def __init__(self, *args, **kwargs): pass
        def save_event(self, event): pass
        def diff(self, *args, **kwargs): return None
        def fork(self, *args, **kwargs): return None
        
    monkeypatch.setattr("sim.main.PostgresChronoDAG", MockChronoDAG)
    
    config = SimConfig(seed=seed, num_users=10, sim_duration_days=1, db_url="postgresql://mock:5432")
    engine, gateway, dag = build_simulation(config)
    
    data_dir = os.path.join(os.path.dirname(__file__), "..", "..", "data", "paysim")
    try:
        params = calibrate_from_csv(data_dir)
    except FileNotFoundError:
        from sim.population.interfaces import CalibratedParams
        params = CalibratedParams({}, [], {}, {}, {})
    
    behaviour_model = PopulationBehaviourModel(params, engine._rng)
    population = PopulationManager(behaviour_model, engine._rng)
    population.create_population(num_users=10, num_merchants=2)
    population.start_agent_loops(engine)
    
    engine._env.run(until=1 * 3600 * 1e9)
    return engine.get_state_hash()

def test_determinism_multi_run(monkeypatch):
    """Verify that multiple runs with the same seed produce the identical state hash."""
    hash1 = run_sim_for_hash(123, monkeypatch)
    hash2 = run_sim_for_hash(123, monkeypatch)
    hash3 = run_sim_for_hash(999, monkeypatch)
    
    assert hash1 == hash2, "Hashes for the same seed should be identical"
    assert hash1 != hash3, "Hashes for different seeds should be different"
