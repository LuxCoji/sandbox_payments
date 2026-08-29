"""Unit tests for sim.gateway.errors — kept separate from test_gateway.py
so the classification logic is tested directly, not only through
ToolGatewayImpl.call_tool()'s except block.
"""
from __future__ import annotations

from sim.gateway.errors import internal_error_result


def test_internal_error_result_is_marked_distinctly_from_a_rejection() -> None:
    result = internal_error_result(ValueError("boom"), tool_name="transfer_funds", actor_id="agent1")

    assert result.success is False
    assert result.tool_name == "transfer_funds"
    assert result.error_code == "INTERNAL_ERROR"
    # The message has to do double duty: tell a human this is a bug (not a
    # declined transaction) and tell an LLM reading it the same thing, so
    # it doesn't reason about INTERNAL_ERROR as if it were a business rule.
    assert result.error_message is not None
    assert "bug in the simulation" in result.error_message
    assert "not a rejection" in result.error_message
    assert "ValueError" in result.error_message
    assert "boom" in result.error_message
