"""Unit tests for identity.py."""
from __future__ import annotations

from typing import TYPE_CHECKING

from agents.redteam.identity import bootstrap_red_agent_context
from sim.gateway.interfaces import ROLE_CAPABILITIES, ActorRole

if TYPE_CHECKING:
    from pathlib import Path


def test_bootstrap_grants_exactly_red_agent_role_capabilities(tmp_path: Path) -> None:
    ctx = bootstrap_red_agent_context(
        branch_id="red-team/session-1", session_id="session-1",
        identity_file=tmp_path / ".persona_identity.json",
    )
    assert ctx.actor_role == ActorRole.RED_AGENT
    assert ctx.capabilities == ROLE_CAPABILITIES[ActorRole.RED_AGENT]
    assert ctx.branch_id == "red-team/session-1"
    assert ctx.session_id == "session-1"


def test_bootstrap_mints_and_persists_actor_id_on_first_use(tmp_path: Path) -> None:
    identity_file = tmp_path / ".persona_identity.json"
    assert not identity_file.exists()

    ctx = bootstrap_red_agent_context(branch_id="main", session_id="s1", identity_file=identity_file)
    assert identity_file.exists()
    assert ctx.actor_id


def test_bootstrap_reuses_persisted_actor_id_across_calls(tmp_path: Path) -> None:
    identity_file = tmp_path / ".persona_identity.json"

    ctx1 = bootstrap_red_agent_context(branch_id="main", session_id="s1", identity_file=identity_file)
    ctx2 = bootstrap_red_agent_context(branch_id="red-team/session-2", session_id="s2", identity_file=identity_file)

    assert ctx1.actor_id == ctx2.actor_id


def test_explicit_persistent_actor_id_overrides_file(tmp_path: Path) -> None:
    identity_file = tmp_path / ".persona_identity.json"
    bootstrap_red_agent_context(branch_id="main", session_id="s1", identity_file=identity_file)

    ctx = bootstrap_red_agent_context(
        branch_id="main", session_id="s1",
        persistent_actor_id="explicit-persona-id",
        identity_file=identity_file,
    )
    assert ctx.actor_id == "explicit-persona-id"
