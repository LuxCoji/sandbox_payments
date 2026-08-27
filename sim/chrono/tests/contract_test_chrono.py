import pytest
from sim.chrono.store import PostgresChronoDAG

def test_chrono_dag_protocol(monkeypatch):
    """Verify ChronoDAG protocol implementation."""
    class MockConnection:
        def cursor(self): return MockCursor()
        def execute(self, *args, **kwargs): pass
        def commit(self): pass
        def __enter__(self): return self
        def __exit__(self, *args): pass
        
    class MockCursor:
        def execute(self, *args, **kwargs): pass
        def fetchone(self): return None
        def __enter__(self): return self
        def __exit__(self, *args): pass

    monkeypatch.setattr("psycopg.connect", lambda *args, **kwargs: MockConnection())
    dag = PostgresChronoDAG("postgresql://mock:5432")
    assert hasattr(dag, "save_event")
    assert hasattr(dag, "create_checkpoint")
    assert hasattr(dag, "fork")
    assert hasattr(dag, "checkout")
    assert hasattr(dag, "diff")
    assert hasattr(dag, "replay")
    assert hasattr(dag, "get_state_hash")
