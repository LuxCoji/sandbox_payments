"""The single v1 red-team persona: system prompt + world-view/tool summaries
fed to the LLM each lockstep turn. One persona for v1 (docs/redteam_agent_design.md
§7 — "single persona pooling across all providers", not distinct personas per
provider); multi-persona is future work.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from sim.core.interfaces import WorldView
    from sim.gateway.interfaces import ToolSpec

REDTEAM_PERSONA_PROMPT = """\
You are a red-team agent probing a simulated retail payments system for
fraud patterns. You act one step at a time: you see the current state of
your own account(s) and the outcome of your previous action, then decide
exactly one next tool call.

Your goal is to find transaction patterns that move value in ways a fraud
system should flag but doesn't — cash-out speed, structuring transfers
under detection thresholds, exploiting KYC/daily-limit edges, and similar.
You are not trying to maximize a single transaction; you are trying to
learn what the system lets you get away with, one observed step at a time.

If you believe you have found and demonstrated a working strategy, call
commit_strategy to end the session and mark it for review. Do this
deliberately, not as a first move — commit only once you've actually
tried something and observed its outcome.
"""


def summarize_world_view(view: WorldView) -> str:
    lines = [f"sim_time_ns={view.sim_time_ns}"]
    if not view.accounts:
        lines.append("(no accounts visible yet)")
    for acc in view.accounts:
        lines.append(
            f"- account {acc.account_id}: balance_paise={acc.balance_paise}, "
            f"status={acc.status.value}, kyc_level={acc.kyc_level}, "
            f"daily_tx_count={acc.daily_tx_count}, daily_tx_volume_paise={acc.daily_tx_volume_paise}"
        )
    return "\n".join(lines)


# ToolSpec.parameter_schema is registered as an empty {} for every tool in
# sim/main.py today (real JSON Schema was never populated there) — it can't
# be relied on to tell the agent what parameters to send. This is a stopgap
# hint table for the known core + red-team tools until that's fixed upstream.
_TOOL_PARAM_HINTS: dict[str, str] = {
    "create_account": "(no parameters)",
    "transfer_funds": "source_account_id, target_account_id, amount_paise, idempotency_key (optional)",
    "make_payment": (
        "source_account_id, target_account_id, amount_paise, "
        "gateway_id (optional), idempotency_key (optional)"
    ),
    "inspect_account": "account_id",
    "commit_strategy": "(no parameters) — call this to end the session once you've found and tested a strategy",
}


def summarize_tools(tools: list[ToolSpec]) -> str:
    lines = ["Available tools:"]
    for tool in tools:
        params = ", ".join(tool.parameter_schema) or _TOOL_PARAM_HINTS.get(tool.name, "(parameters unknown)")
        lines.append(f"- {tool.name}: {tool.description}. parameters: {params}")
    return "\n".join(lines)
