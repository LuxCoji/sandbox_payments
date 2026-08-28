"""Confirms the "step zero" claim in docs/redteam_agent_design.md: a
RED_AGENT ActorContext built via bootstrap_red_agent_context() gets a real,
correctly-filtered WorldView through WorldEngine.get_world_view() — not just
that the ActorContext dataclass constructs without error.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from agents.redteam.identity import bootstrap_red_agent_context
from sim.core.engine import WorldEngineImpl
from sim.core.interfaces import AccountType
from sim.scheduler.env import SimulationEnv
from sim.scheduler.rng import DeterministicRNG

if TYPE_CHECKING:
    from pathlib import Path


def test_red_agent_sees_only_its_own_accounts(tmp_path: Path) -> None:
    engine = WorldEngineImpl(env=SimulationEnv(), rng=DeterministicRNG.from_seed(1))
    ctx = bootstrap_red_agent_context(
        branch_id="main", session_id="s1", identity_file=tmp_path / ".persona_identity.json",
    )

    engine.create_account(
        account_id="agent-acc", owner_id=ctx.actor_id,
        account_type=AccountType.PERSONAL, initial_balance_paise=5_000, kyc_level=1,
    )
    engine.create_account(
        account_id="other-acc", owner_id="someone-else",
        account_type=AccountType.PERSONAL, initial_balance_paise=99_999, kyc_level=1,
    )

    view = engine.get_world_view(ctx.actor_id, ctx.actor_role)

    assert {a.account_id for a in view.accounts} == {"agent-acc"}
    assert view.accounts[0].balance_paise == 5_000
