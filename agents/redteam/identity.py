"""RED_AGENT actor identity bootstrap.

The first production code anywhere in this repo that constructs a
RED_AGENT ActorContext — before this, only a unit test
(sim/gateway/tests/test_gateway.py) ever built one. See
docs/redteam_agent_design.md §6 Phase 4.

Persona identity is persisted locally (gitignored), not in sim or the
ChronoDAG — it's harness bookkeeping, not simulation state, kept stable
across sessions so the same persona's actor_id shows up consistently across
red-team branches for diff-branches attribution.
"""
from __future__ import annotations

import json
import uuid
from pathlib import Path

from sim.gateway.interfaces import ROLE_CAPABILITIES, ActorContext, ActorRole

_IDENTITY_FILE = Path(__file__).parent / ".persona_identity.json"


def _load_persisted_actor_id(path: Path) -> str | None:
    if not path.exists():
        return None
    data = json.loads(path.read_text())
    actor_id = data.get("actor_id")
    return actor_id if isinstance(actor_id, str) else None


def _persist_actor_id(path: Path, actor_id: str) -> None:
    path.write_text(json.dumps({"actor_id": actor_id}))


def bootstrap_red_agent_context(
    branch_id: str,
    session_id: str,
    persistent_actor_id: str | None = None,
    identity_file: Path = _IDENTITY_FILE,
) -> ActorContext:
    """Build the RED_AGENT ActorContext for a session.

    actor_id resolution order: explicit `persistent_actor_id` argument, then
    whatever's already persisted in `identity_file`, then a freshly minted
    id (persisted for next time). Random (uuid4), not the uuid5 name-derived
    scheme sim/ uses for engine-internal ids (event_id, tx_id) — this id
    doesn't feed the core determinism contract the way those do: red-team
    sessions are driven by live LLM calls against real provider APIs and are
    not expected to be bit-for-bit reproducible run to run the way "main"
    is, so there's no determinism property here to preserve.
    """
    actor_id = persistent_actor_id or _load_persisted_actor_id(identity_file)
    if actor_id is None:
        actor_id = str(uuid.uuid4())
        _persist_actor_id(identity_file, actor_id)

    return ActorContext(
        actor_id=actor_id,
        actor_role=ActorRole.RED_AGENT,
        capabilities=ROLE_CAPABILITIES[ActorRole.RED_AGENT],
        branch_id=branch_id,
        session_id=session_id,
    )
