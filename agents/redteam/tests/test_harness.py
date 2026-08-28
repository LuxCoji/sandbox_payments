"""Unit test for the lockstep session loop — router.completion() is mocked
(scripted JSON responses), no real LLM/network calls. Uses InMemoryChronoDAG
in place of PostgresChronoDAG, same pattern as
tests/integration/test_build_simulation_for_branch.py.
"""
from __future__ import annotations

import json
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest

from agents.redteam.config import RedTeamConfig
from agents.redteam.harness import run_session
from agents.redteam.llm_router import build_router
from sim.chrono.tests._fake_dag import InMemoryChronoDAG
from sim.config import SimConfig
from sim.core.interfaces import AccountType
from sim.main import build_simulation

if TYPE_CHECKING:
    from pathlib import Path

    from sim.chrono.interfaces import Checkpoint


def _fake_response(tool_name: str, parameters: dict[str, object] | None = None) -> SimpleNamespace:
    content = json.dumps({"tool_name": tool_name, "parameters": parameters or {}, "reasoning": "test"})
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content))])


@pytest.fixture
def shared_dag(monkeypatch: pytest.MonkeyPatch) -> InMemoryChronoDAG:
    dag = InMemoryChronoDAG()
    monkeypatch.setattr("sim.main.PostgresChronoDAG", lambda db_url=None: dag)
    monkeypatch.setattr("agents.redteam.harness.PostgresChronoDAG", lambda db_url=None: dag)
    return dag


@pytest.fixture
def router_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> RedTeamConfig:
    providers_file = tmp_path / "providers.yaml"
    providers_file.write_text(
        "deployments:\n"
        "  - provider: groq\n"
        "    litellm_model: groq/llama-3.3-70b-versatile\n"
        "    api_key_env: TEST_GROQ_KEY\n"
        "    rpm: 30\n"
    )
    monkeypatch.setenv("TEST_GROQ_KEY", "fake-key")
    return RedTeamConfig(providers_file=providers_file, session_max_steps=5, enable_otel_tracing=False)


def _make_checkpoint(sim_config: SimConfig) -> Checkpoint:
    engine, _gateway, chrono = build_simulation(sim_config)
    engine.create_account(
        account_id="acc-1", owner_id="placeholder-owner",
        account_type=AccountType.PERSONAL, initial_balance_paise=10_000, kyc_level=1,
    )
    checkpoint: Checkpoint = chrono.create_checkpoint(
        branch_id="main", event_number=engine._seq_num, sim_time_ns=engine.sim_time_ns,
        state_hash=engine.get_state_hash(), aggregate_snapshot=engine.get_full_snapshot_bytes(),
        rng_state=engine._rng.get_state(),
    )
    return checkpoint


def test_run_session_stops_on_commit_strategy(
    shared_dag: InMemoryChronoDAG, router_config: RedTeamConfig, tmp_path: Path
) -> None:
    sim_config = SimConfig(seed=42, db_url="postgresql://mock:5432")
    checkpoint = _make_checkpoint(sim_config)

    router = build_router(router_config)
    scripted = [
        _fake_response("inspect_account", {"account_id": "acc-1"}),
        _fake_response("commit_strategy"),
    ]
    with patch.object(router, "completion", side_effect=scripted):
        result = run_session(
            sim_config, router_config, warmup_checkpoint_id=checkpoint.checkpoint_id,
            session_id="test-session", router=router,
            identity_file=tmp_path / ".persona_identity.json",
        )

    assert result.steps_taken == 2
    assert result.committed is True
    assert result.branch_id == "red-team/test-session"
    assert result.step_log[0]["tool_name"] == "inspect_account"
    assert result.step_log[1]["tool_name"] == "commit_strategy"
    assert all(entry["success"] for entry in result.step_log)


def test_run_session_stops_at_step_cap_without_commit(
    shared_dag: InMemoryChronoDAG, router_config: RedTeamConfig, tmp_path: Path
) -> None:
    sim_config = SimConfig(seed=42, db_url="postgresql://mock:5432")
    checkpoint = _make_checkpoint(sim_config)

    small_config = RedTeamConfig(
        providers_file=router_config.providers_file, session_max_steps=3, enable_otel_tracing=False
    )
    router = build_router(small_config)
    scripted = [_fake_response("inspect_account", {"account_id": "acc-1"}) for _ in range(3)]
    with patch.object(router, "completion", side_effect=scripted):
        result = run_session(
            sim_config, small_config, warmup_checkpoint_id=checkpoint.checkpoint_id,
            session_id="test-session-2", router=router,
            identity_file=tmp_path / ".persona_identity.json",
        )

    assert result.steps_taken == 3
    assert result.committed is False


def test_run_session_requires_checkpoint_or_genesis(
    shared_dag: InMemoryChronoDAG, router_config: RedTeamConfig
) -> None:
    sim_config = SimConfig(seed=42, db_url="postgresql://mock:5432")
    with pytest.raises(ValueError, match="warmup_checkpoint_id"):
        run_session(sim_config, router_config)
