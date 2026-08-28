"""LangGraphAdapter tests."""
from __future__ import annotations

from typing import TYPE_CHECKING, cast

import pytest

from sim.gateway.adapters import LangGraphAdapter
from sim.gateway.gateway import ToolGatewayImpl
from sim.gateway.interfaces import ActorContext, ActorRole, ToolSpec
from sim.gateway.policy import RateLimiter
from sim.gateway.registry import ToolRegistry

if TYPE_CHECKING:
    from sim.core.interfaces import WorldEngine


class _FakeEngine:
    def __init__(self) -> None:
        self.sim_time_ns = 0.0


def _build_gateway() -> ToolGatewayImpl:
    registry = ToolRegistry()
    registry.register_tool(
        ToolSpec("noop_tool", "does nothing", frozenset(), {}),
        lambda context, params, engine: [],
    )
    return ToolGatewayImpl(registry=registry, rate_limiter=RateLimiter(), engine=cast("WorldEngine", _FakeEngine()))


def test_as_tool_node_calls_gateway_and_returns_tool_result() -> None:
    gateway = _build_gateway()
    ctx = ActorContext(actor_id="agent1", actor_role=ActorRole.RED_AGENT, capabilities=frozenset(), branch_id="main")
    node = LangGraphAdapter(gateway).as_tool_node(ctx)

    state = {"action": {"tool_name": "noop_tool", "parameters": {}}}
    update = node(state)

    assert set(update) == {"tool_result"}
    assert update["tool_result"].success is True
    assert update["tool_result"].tool_name == "noop_tool"


def test_as_tool_node_rejects_non_mapping_action() -> None:
    gateway = _build_gateway()
    ctx = ActorContext(actor_id="agent1", actor_role=ActorRole.RED_AGENT, capabilities=frozenset(), branch_id="main")
    node = LangGraphAdapter(gateway).as_tool_node(ctx)

    with pytest.raises(TypeError, match="mapping"):
        node({"action": "not-a-mapping"})
