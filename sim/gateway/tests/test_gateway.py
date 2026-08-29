"""Gateway tests."""
from __future__ import annotations

from typing import TYPE_CHECKING, cast

from sim.gateway.gateway import ToolGatewayImpl
from sim.gateway.interfaces import ActorContext, ActorRole, Capability, ToolSpec
from sim.gateway.policy import RateLimiter, check_capabilities
from sim.gateway.registry import ToolRegistry

if TYPE_CHECKING:
    from sim.core.interfaces import WorldEngine


class _FakeEngine:
    """Minimal stand-in for WorldEngine — call_tool only reads sim_time_ns.
    Cast to WorldEngine at the call site rather than implementing the full
    five-method Protocol, which these tests never exercise.
    """

    def __init__(self) -> None:
        self.sim_time_ns = 0.0


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


def test_tier_rate_limit_independent_of_per_tool_limit() -> None:
    limiter = RateLimiter()

    # "branch_op" tier caps at 2/step regardless of per-tool limits — two
    # different tool names sharing the tier both draw from the same budget.
    assert limiter.check_and_increment_tier("agent1", "branch_op", 0) is None
    assert limiter.check_and_increment_tier("agent1", "branch_op", 0) is None
    err = limiter.check_and_increment_tier("agent1", "branch_op", 0)
    assert err is not None
    assert "per step" in err

    # "normal" tier has no cap (TIER_LIMITS["normal"] = (None, None))
    for _ in range(50):
        assert limiter.check_and_increment_tier("agent1", "normal", 0) is None


def test_call_tool_enforces_tier_limit_across_different_tools() -> None:
    registry = ToolRegistry()
    for name in ("fork_branch", "diff_branches"):
        registry.register_tool(
            ToolSpec(name, "test", frozenset(), {}, rate_limit_tier="branch_op"),
            lambda context, params, engine: [],
        )

    gateway = ToolGatewayImpl(registry=registry, rate_limiter=RateLimiter(), engine=cast("WorldEngine", _FakeEngine()))
    ctx = ActorContext(actor_id="agent1", actor_role=ActorRole.RED_AGENT, capabilities=frozenset(), branch_id="main")

    assert gateway.call_tool("fork_branch", {}, ctx).success is True
    assert gateway.call_tool("diff_branches", {}, ctx).success is True
    # Third branch_op call of any kind this step hits the shared tier cap.
    result = gateway.call_tool("fork_branch", {}, ctx)
    assert result.success is False
    assert result.error_code == "RATE_LIMITED"


def test_call_tool_reports_a_raised_bug_as_internal_error_not_a_rejection() -> None:
    """A handler that raises something other than ToolRejection is a real
    bug, not a business decline — call_tool() must route it through
    sim.gateway.errors.internal_error_result() (see test_errors.py),
    distinctly from a ToolRejection's error_code/message.
    """
    registry = ToolRegistry()

    def buggy_handler(context, params, engine):
        raise ValueError("unexpected")

    registry.register_tool(ToolSpec("buggy_tool", "test", frozenset(), {}), buggy_handler)

    gateway = ToolGatewayImpl(registry=registry, rate_limiter=RateLimiter(), engine=cast("WorldEngine", _FakeEngine()))
    ctx = ActorContext(actor_id="agent1", actor_role=ActorRole.RED_AGENT, capabilities=frozenset(), branch_id="main")

    result = gateway.call_tool("buggy_tool", {}, ctx)

    assert result.success is False
    assert result.error_code == "INTERNAL_ERROR"
    assert result.error_message is not None
    assert "bug in the simulation" in result.error_message
