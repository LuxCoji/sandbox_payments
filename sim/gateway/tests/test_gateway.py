"""Gateway tests."""
from __future__ import annotations

from sim.gateway.interfaces import ActorContext, ActorRole, Capability, ToolSpec
from sim.gateway.policy import RateLimiter, check_capabilities
from sim.gateway.registry import ToolRegistry


def test_tool_registry() -> None:
    registry = ToolRegistry()
    spec = ToolSpec(
        name="test_tool",
        description="A test tool",
        required_capabilities=frozenset({Capability.VIEW_OWN_ACCOUNT}),
        parameter_schema={},
    )

    def handler(context, params, engine): return []

    registry.register_tool(spec, handler)
    assert registry.get_tool("test_tool") is not None


def test_capability_check() -> None:
    ctx = ActorContext(
        actor_id="user1", actor_role=ActorRole.USER,
        capabilities=frozenset({Capability.VIEW_OWN_ACCOUNT}),
        branch_id="main"
    )

    # Has capability
    assert check_capabilities(ctx, frozenset({Capability.VIEW_OWN_ACCOUNT})) is None

    # Missing capability
    assert check_capabilities(ctx, frozenset({Capability.FREEZE_ACCOUNT})) is not None


def test_rate_limiter() -> None:
    limiter = RateLimiter()

    # First call allowed
    err = limiter.check_and_increment("user1", "tool1", 0, limit_per_step=1, limit_per_day=None)
    assert err is None

    # Second call in same step blocked
    err = limiter.check_and_increment("user1", "tool1", 0, limit_per_step=1, limit_per_day=None)
    assert err is not None
