"""Gateway subsystem contracts.

Defines the ToolGateway protocol for capability-gated, rate-limited
tool execution within the simulation.
"""
from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from sim.core.interfaces import ActorRole

if TYPE_CHECKING:
    from collections.abc import Callable


class Capability(enum.Enum):
    """Capabilities that can be granted to actors.

    Each tool requires specific capabilities to execute.
    Capabilities are assigned per ActorRole.
    """
    # Account operations
    CREATE_ACCOUNT = "CREATE_ACCOUNT"
    VIEW_OWN_ACCOUNT = "VIEW_OWN_ACCOUNT"
    VIEW_ALL_ACCOUNTS = "VIEW_ALL_ACCOUNTS"
    FREEZE_ACCOUNT = "FREEZE_ACCOUNT"
    CLOSE_ACCOUNT = "CLOSE_ACCOUNT"

    # Transaction operations
    MAKE_PAYMENT = "MAKE_PAYMENT"
    TRANSFER_FUNDS = "TRANSFER_FUNDS"
    REFUND_PAYMENT = "REFUND_PAYMENT"
    INITIATE_CHARGEBACK = "INITIATE_CHARGEBACK"

    # Device operations
    REGISTER_DEVICE = "REGISTER_DEVICE"
    BLOCK_DEVICE = "BLOCK_DEVICE"

    # Merchant operations
    ONBOARD_MERCHANT = "ONBOARD_MERCHANT"
    SUSPEND_MERCHANT = "SUSPEND_MERCHANT"

    # Observation operations
    VIEW_TRANSACTIONS = "VIEW_TRANSACTIONS"
    VIEW_ALL_TRANSACTIONS = "VIEW_ALL_TRANSACTIONS"
    VIEW_RISK_SCORES = "VIEW_RISK_SCORES"
    VIEW_ALERTS = "VIEW_ALERTS"

    # Chrono operations
    FORK_BRANCH = "FORK_BRANCH"
    REPLAY_BRANCH = "REPLAY_BRANCH"
    DIFF_BRANCHES = "DIFF_BRANCHES"

    # System operations
    INSPECT_ANY_ACCOUNT = "INSPECT_ANY_ACCOUNT"
    MODIFY_GLOBAL_PARAMS = "MODIFY_GLOBAL_PARAMS"


# ── Default capability grants per role ────────────────────────────────────

ROLE_CAPABILITIES: dict[ActorRole, frozenset[Capability]] = {
    ActorRole.USER: frozenset({
        Capability.VIEW_OWN_ACCOUNT,
        Capability.MAKE_PAYMENT,
        Capability.TRANSFER_FUNDS,
        Capability.REGISTER_DEVICE,
        Capability.VIEW_TRANSACTIONS,
    }),
    ActorRole.MERCHANT: frozenset({
        Capability.VIEW_OWN_ACCOUNT,
        Capability.REFUND_PAYMENT,
        Capability.VIEW_TRANSACTIONS,
        Capability.ONBOARD_MERCHANT,
    }),
    ActorRole.BANK_OPS: frozenset({
        Capability.VIEW_ALL_ACCOUNTS,
        Capability.FREEZE_ACCOUNT,
        Capability.CLOSE_ACCOUNT,
        Capability.BLOCK_DEVICE,
        Capability.SUSPEND_MERCHANT,
        Capability.VIEW_ALL_TRANSACTIONS,
        Capability.VIEW_RISK_SCORES,
    }),
    ActorRole.RISK_ANALYST: frozenset({
        Capability.VIEW_ALL_TRANSACTIONS,
        Capability.VIEW_RISK_SCORES,
        Capability.VIEW_ALERTS,
    }),
    ActorRole.RED_AGENT: frozenset({
        Capability.VIEW_OWN_ACCOUNT,
        # WorldEngine.get_world_view() now grants RED_AGENT the same
        # branch-wide (PII-masked) account visibility as BANK_OPS/
        # RISK_ANALYST/BLUE_AGENT — deliberately white-box, see that
        # method's docstring. Listed here for documentation accuracy;
        # get_world_view() is called directly by the harness today, not
        # gated through ToolGateway.call_tool(), so this isn't yet an
        # enforced check — it records intent for when/if it is.
        Capability.VIEW_ALL_ACCOUNTS,
        Capability.MAKE_PAYMENT,
        Capability.TRANSFER_FUNDS,
        Capability.REGISTER_DEVICE,
        Capability.VIEW_TRANSACTIONS,
        Capability.CREATE_ACCOUNT,
        # Branch ops — the agent's own exploratory-forking instrument, see
        # docs/redteam_agent_design.md §3. Rate-limited separately via
        # ToolSpec.rate_limit_tier="branch_op", these are not "free" just
        # because they're granted.
        Capability.FORK_BRANCH,
        Capability.REPLAY_BRANCH,
        Capability.DIFF_BRANCHES,
    }),
    ActorRole.BLUE_AGENT: frozenset({
        Capability.VIEW_ALL_ACCOUNTS,
        Capability.VIEW_ALL_TRANSACTIONS,
        Capability.VIEW_RISK_SCORES,
        Capability.VIEW_ALERTS,
        Capability.FREEZE_ACCOUNT,
        Capability.BLOCK_DEVICE,
        Capability.INITIATE_CHARGEBACK,
    }),
}


@dataclass(frozen=True)
class ToolSpec:
    """Specification for a registered simulation tool."""
    name: str                                # Unique tool name (e.g., "transfer_funds")
    description: str
    required_capabilities: frozenset[Capability]
    parameter_schema: dict[str, object]      # JSON Schema for tool parameters
    rate_limit_per_step: int | None = None   # Max calls per sim step (None = unlimited)
    rate_limit_per_day: int | None = None    # Max calls per sim day (None = unlimited)
    visible_fields: dict[ActorRole, frozenset[str]] = field(default_factory=dict)
                                             # Role → set of output fields visible
    rate_limit_tier: str = "normal"          # Additional tier-wide cap, keyed independently
                                             # of the per-tool limits above — see
                                             # sim.gateway.policy.TIER_LIMITS. "branch_op"
                                             # tools (fork/checkout/diff/commit) are far more
                                             # expensive than a payment call and share one
                                             # tighter budget regardless of which specific
                                             # branch tool is invoked.


@dataclass(frozen=True)
class ActorContext:
    """Context identifying the calling actor for authorization."""
    actor_id: str                            # UUIDv7
    actor_role: ActorRole
    capabilities: frozenset[Capability]
    branch_id: str                           # Current ChronoDAG branch
    device_id: str | None = None
    session_id: str | None = None


class ToolRejection(Exception):
    """A tool handler raises this to report a *business* rejection — the
    request was well-formed and reached the domain logic, but the domain
    logic said no (insufficient funds, over a daily limit, account not
    found, ...) — as distinct from a genuine bug/crash.

    ToolGatewayImpl.call_tool() catches this specifically (before the
    generic `except Exception`) and threads `error_code` straight into
    ToolResult.error_code, instead of every rejection collapsing into the
    same uninformative "INTERNAL_ERROR" a real crash would also produce.
    That distinction matters most for a red-team agent: "INTERNAL_ERROR"
    tells it nothing about which control it just tripped, while
    "LIMIT_EXCEEDED" or "INSUFFICIENT_FUNDS" is the exact signal it needs to
    plan its next move.
    """

    def __init__(self, error_code: str, message: str) -> None:
        super().__init__(message)
        self.error_code = error_code


@dataclass(frozen=True)
class ToolResult:
    """Result of a tool execution."""
    success: bool
    tool_name: str
    data: dict[str, object] = field(default_factory=dict)
    error_code: str | None = None
    error_message: str | None = None
    filtered_fields: tuple[str, ...] = ()    # Fields removed due to visibility rules


@runtime_checkable
class ToolGateway(Protocol):
    """Protocol for the capability-gated tool execution gateway.

    All agent interactions with the simulation flow through this gateway.
    It enforces:
        1. Capability authorization
        2. Token-bucket rate limiting per actor and tool
        3. Output field visibility filtering based on actor role
    """

    def register_tool(self, spec: ToolSpec, handler: Callable) -> None:
        """Register a new tool with the gateway."""
        ...

    def list_tools(self, context: ActorContext) -> list[ToolSpec]:
        """List tools available to the given actor (filtered by capabilities)."""
        ...

    def call_tool(
        self,
        tool_name: str,
        parameters: dict[str, object],
        context: ActorContext,
    ) -> ToolResult:
        """Execute a tool within the simulation context.

        1. Check actor has required capabilities for the tool.
        2. Check rate limits (per-step and per-day).
        3. Execute the tool handler.
        4. Filter output fields based on actor role visibility.

        Returns ToolResult with success/failure and filtered data.
        """
        ...
