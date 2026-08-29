"""Classification + logging for exceptions a tool handler raises.

Kept separate from ToolGatewayImpl.call_tool() so this is independently
testable and has exactly one job: turn "a handler blew up" into (a) a
server-side log an operator can actually debug from, and (b) a ToolResult
whose error_message doesn't read the same as a normal business rejection.

Two different failure shapes reach call_tool()'s except blocks:
  - ToolRejection: the handler understood the request and the domain said
    no (insufficient funds, over a daily limit, ...) — handled inline in
    call_tool() itself, never reaches this module.
  - Anything else: a genuine bug — wrong assumption, unhandled edge case,
    a real crash. That's what internal_error_result() is for. Before this
    module existed, both cases could look identical to whoever read the
    error_code/message (a raw Python exception message, no traceback
    logged anywhere, "INTERNAL_ERROR" the only signal) — indistinguishable
    to a human watching the UI, and actively misleading to a red-team LLM
    reading it as if it were a rejected transaction.
"""
from __future__ import annotations

from sim.gateway.interfaces import ToolResult
from sim.observability import get_logger

logger = get_logger("finsim.gateway.errors")


def internal_error_result(exc: Exception, *, tool_name: str, actor_id: str) -> ToolResult:
    """Log a handler's unexpected exception with a full traceback, and
    return the ToolResult that should reach the caller in its place.

    The error_message is written for two different readers at once: a
    human watching the live step feed (this is a bug, not the agent doing
    something wrong) and the LLM deciding what to do next (don't reason
    about this as if it were a declined transaction — it isn't one).
    """
    logger.error(
        "Tool handler raised an unexpected exception",
        tool_name=tool_name, actor_id=actor_id,
        exception_type=type(exc).__name__, exc_info=True,
    )
    return ToolResult(
        success=False,
        tool_name=tool_name,
        error_code="INTERNAL_ERROR",
        error_message=(
            f"System error while running '{tool_name}' — this is a bug in the simulation "
            f"itself, not a rejection of your request ({type(exc).__name__}: {exc}). "
            "It has been logged for investigation; treat it as noise and try something else."
        ),
    )
