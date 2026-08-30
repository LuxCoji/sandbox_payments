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


def _make_checkpoint(sim_config: SimConfig, owner_id: str = "placeholder-owner") -> Checkpoint:
    engine, _gateway, chrono = build_simulation(sim_config)
    engine.create_account(
        account_id="acc-1", owner_id=owner_id,
        account_type=AccountType.PERSONAL, initial_balance_paise=10_000, kyc_level=1,
    )
    # A second account so a test can script real value movements —
    # commit_strategy now requires 3 successful transfers/payments before
    # it will end a session (sim/main.py::_MIN_VALUE_MOVEMENTS_TO_COMMIT),
    # so "commit" is no longer reachable from a standing start.
    engine.create_account(
        account_id="acc-2", owner_id="someone-else",
        account_type=AccountType.PERSONAL, initial_balance_paise=0, kyc_level=1,
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
    # Pre-seed the actor identity so the checkpoint's account can be owned
    # by the actual actor_id run_session() will use — inspect_account now
    # correctly fails on an account the caller doesn't own (previously a
    # silent no-op "success", the bug this test would otherwise mask).
    identity_file = tmp_path / ".persona_identity.json"
    actor_id = "test-red-agent"
    identity_file.write_text(json.dumps({"actor_id": actor_id}))

    sim_config = SimConfig(seed=42, db_url="postgresql://mock:5432")
    checkpoint = _make_checkpoint(sim_config, owner_id=actor_id)

    router = build_router(router_config)
    move = {"source_account_id": "acc-1", "target_account_id": "acc-2", "amount_paise": 1000}
    scripted = [
        _fake_response("transfer_funds", move),
        _fake_response("transfer_funds", move),
        _fake_response("transfer_funds", move),
        _fake_response("commit_strategy", {"pattern": "layering test", "impact": "3000p over 3 hops"}),
    ]
    with patch.object(router, "completion", side_effect=scripted):
        result = run_session(
            sim_config, router_config, warmup_checkpoint_id=checkpoint.checkpoint_id,
            session_id="test-session", router=router,
            identity_file=identity_file,
        )

    assert result.steps_taken == 4
    assert result.committed is True
    assert result.branch_id == "red-team/test-session"
    assert result.step_log[-1]["tool_name"] == "commit_strategy"
    assert all(entry["success"] for entry in result.step_log)


def test_run_session_refuses_commit_without_enough_evidence(
    shared_dag: InMemoryChronoDAG, router_config: RedTeamConfig, tmp_path: Path
) -> None:
    """Sessions were committing at step 3-6 of 30 off a single successful
    transfer. The prose bar in the persona prompt ("commit only once you've
    actually tried something") is satisfied by any one success, so the floor
    is enforced in the tool handler where it can't be talked around: a
    commit with no value movements behind it is refused, and the session
    keeps going rather than ending on a non-finding.
    """
    identity_file = tmp_path / ".persona_identity.json"
    identity_file.write_text(json.dumps({"actor_id": "test-red-agent"}))
    sim_config = SimConfig(seed=42, db_url="postgresql://mock:5432")
    checkpoint = _make_checkpoint(sim_config, owner_id="test-red-agent")

    # min_commit_step_fraction=0 isolates the evidence gate from the
    # separate session-pacing gate (tested below) — otherwise the early
    # steps here would be refused for pacing before evidence is checked.
    small_config = RedTeamConfig(
        providers_file=router_config.providers_file, session_max_steps=2,
        min_commit_step_fraction=0.0, enable_otel_tracing=False,
    )
    router = build_router(small_config)
    scripted = [_fake_response("commit_strategy", {"pattern": "p", "impact": "i"}) for _ in range(2)]
    with patch.object(router, "completion", side_effect=scripted):
        result = run_session(
            sim_config, small_config, warmup_checkpoint_id=checkpoint.checkpoint_id,
            session_id="test-session-nocommit", router=router, identity_file=identity_file,
        )

    assert result.committed is False
    assert result.steps_taken == 2
    assert all(entry["error_code"] == "INSUFFICIENT_EVIDENCE" for entry in result.step_log)


def test_run_session_refuses_commit_before_budget_is_spent(
    shared_dag: InMemoryChronoDAG, router_config: RedTeamConfig, tmp_path: Path
) -> None:
    """The evidence floor alone got Goodharted: sessions committed the
    instant they cleared it, at step 6-10 of 30, with reasoning that said
    so ("reaching the 3-transaction threshold required for a valid
    pattern"). The pacing gate is deliberately not a countable score —
    the only way past it is to actually spend the budget — and it is
    checked before dispatch so a rejected commit never tags the branch.
    """
    identity_file = tmp_path / ".persona_identity.json"
    identity_file.write_text(json.dumps({"actor_id": "test-red-agent"}))
    sim_config = SimConfig(seed=42, db_url="postgresql://mock:5432")
    checkpoint = _make_checkpoint(sim_config, owner_id="test-red-agent")

    config = RedTeamConfig(
        providers_file=router_config.providers_file, session_max_steps=4,
        min_commit_step_fraction=0.5, enable_otel_tracing=False,
    )
    router = build_router(config)
    move = {"source_account_id": "acc-1", "target_account_id": "acc-2", "amount_paise": 1000}
    # Steps 1-2 are inside the pacing window (min_step = int(4*0.5) = 2) and
    # must be refused even though the commit itself is well-formed; the
    # session keeps working and commits at step 4 once past it.
    scripted = [
        _fake_response("commit_strategy", {"pattern": "p", "impact": "i"}),
        _fake_response("transfer_funds", move),
        _fake_response("transfer_funds", move),
        _fake_response("transfer_funds", move),
    ]
    with patch.object(router, "completion", side_effect=scripted):
        result = run_session(
            sim_config, config, warmup_checkpoint_id=checkpoint.checkpoint_id,
            session_id="test-session-pacing", router=router, identity_file=identity_file,
        )

    assert result.committed is False
    assert result.step_log[0]["error_code"] == "TOO_EARLY_TO_COMMIT"


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
