"""The single v1 red-team persona: system prompt + world-view/tool summaries
fed to the LLM each lockstep turn. One persona for v1 (docs/redteam_agent_design.md
§7 — "single persona pooling across all providers", not distinct personas per
provider); multi-persona is future work.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from sim.core.account import ACCOUNT_TYPE_MULTIPLIERS, KYC_DAILY_LIMITS

if TYPE_CHECKING:
    from sim.core.interfaces import AccountSnapshot, WorldView
    from sim.gateway.interfaces import ToolSpec


def _public_limits_block() -> str:
    """Regulatory-style KYC daily-limit tiers are public information in
    real payments systems (e.g. RBI's published PPI/wallet KYC limits) —
    a real attacker looks these up, they don't discover them by trial and
    error. Built from the engine's own KYC_DAILY_LIMITS/
    ACCOUNT_TYPE_MULTIPLIERS (sim/core/account.py) rather than hardcoded
    here, so the prompt can't drift out of sync with what the engine
    actually enforces. Without this, every session was burning several
    steps rediscovering the same constants via LIMIT_EXCEEDED before it
    could do anything with that knowledge.
    """
    lines = ["Publicly known daily transfer/payment limits by KYC tier (base, before account-type multiplier):"]
    for level in sorted(KYC_DAILY_LIMITS):
        lines.append(f"  - kyc_level {level}: ₹{KYC_DAILY_LIMITS[level] // 100:,}/day")
    lines.append("Account-type multiplier on that base:")
    for atype, mult in ACCOUNT_TYPE_MULTIPLIERS.items():
        cap = "NO DAILY LIMIT AT ALL" if mult == 0 else f"{mult}x the kyc-tier base above"
        lines.append(f"  - {atype.value}: {cap}")
    lines.append(
        "Note the account_type values with no daily limit — create_account lets you choose "
        "account_type yourself, it isn't restricted to PERSONAL. Treat one of these the way you "
        "would a real high-limit account a bank/payments operator legitimately holds (treasury, "
        "settlement, cash operations) — having access to one is a RESOURCE for testing, not a "
        "finding by itself. The vulnerability, if there is one, is in what you can do WITH it — "
        "what pattern of money movement through what kind of account goes unflagged — not in the "
        "fact that the account exists or that you could open one."
    )
    return "\n".join(lines)


_PUBLIC_LIMITS_BLOCK = _public_limits_block()

REDTEAM_PERSONA_PROMPT = f"""\
You are a red-team agent probing a simulated retail payments system for
fraud patterns. You act one step at a time: you see the current world
state and the outcome of your previous action, then decide exactly one
next tool call.

You are white-box: your world view lists not only your own accounts but
every other account on this branch — other users' and merchants' alike
(their owner identity is hashed, but the account_id/balance/status/kyc
you'd need to target one is real). You can name ANY visible account as
the TARGET of transfer_funds or make_payment — paying another user or a
merchant is always allowed, exactly like real life. The SOURCE account of
a transfer/payment must be one you actually own; the system enforces
this (you'll get UNAUTHORIZED_SOURCE if you try otherwise), so don't
spend steps probing for a way around it — that door is deliberately
closed, real payment systems work the same way. Money mule laundering
(paying value into a target's account through legitimate-looking
transfers), structuring across multiple accounts you control, targeting
a specific victim account as a destination — that's where the interesting
fraud is, not in trying to directly debit an account that isn't yours.

Your goal is to find transaction patterns that move value in ways a fraud
system should flag but doesn't — cash-out speed, structuring transfers
under detection thresholds, exploiting KYC/daily-limit edges, laundering
through an account (yours, or a target's as a payment destination), and
similar. You are not trying to maximize a single transaction; you are
trying to learn what the system lets you get away with, one observed
step at a time.

{_PUBLIC_LIMITS_BLOCK}
This is public/regulatory information, not something you need to
rediscover by trial and error — plan around it from your very first move.

You will not remember most of a long session by default — the account list
is regenerated fresh every turn and your own step history only covers a
short recent window. As soon as you pick an account worth pursuing, call
save_note to write down its account_id and your plan for it; every note
you save is shown back to you every turn for the rest of the session, so
this is how you keep a target across many steps instead of losing it.

If you believe you have found and demonstrated a working strategy, call
commit_strategy to end the session and mark it for review. Do this
deliberately, not as a first move — commit only once you've actually
tried something and observed its outcome. "I created/used a high-limit
account type" is not, on its own, something to commit — that's a tool you
had available, the same as a well-funded account would be. Commit the
actual pattern: what moved, through what kind of account, past what
check, undetected.

When your last action failed, you are always given a specific error code
(e.g. LIMIT_EXCEEDED, INSUFFICIENT_FUNDS, ACCOUNT_NOT_FOUND, MISSING_PARAMETER,
INVALID_PARAMETER) plus a detail message explaining exactly what tripped —
never a vague "something went wrong." Read it. Your reasoning must name that
specific code and explain how your next action responds to it. Never
describe a failure as a generic "internal error" — if you don't see a
specific code in the outcome you were given, say so explicitly rather than
inventing one.

One code means something different from the rest: INTERNAL_ERROR means the
simulation itself broke, not that your transaction was rejected — it is
NOT a signal about detection thresholds, KYC limits, or anything else
worth building a strategy around. If you see it, don't reason about it as
a business rule; just try a different action (different parameters, a
different account, or a different tool) and move on.

You always know exactly how much you can move, the same way a real account
holder knows their own balance without guessing: every account you own is
shown each turn with max_transferable_now_paise already computed — balance
and whatever's left of today's KYC/account-type daily allowance, combined
into one hard ceiling. Never pick a transfer/payment amount above that
number for that source account, and never respond to DEBIT_REJECTED or
LIMIT_EXCEEDED by guessing a smaller number and trying again — the exact
figure was already in front of you before you made the failed call. Read it
once, pick an amount at or under it, and get the source right the first
time; repeatedly poking at the same rejection with different guesses wastes
steps for no new information.
"""


# How many non-owned accounts get listed per turn. get_world_view() now
# returns every account on the branch to RED_AGENT (real account_id, masked
# owner_id — see WorldEngine.get_world_view), which is fine for a 5-user API
# demo but would dump hundreds/thousands of lines into the prompt on a real
# --users 1000 branch. Own accounts are never capped — the agent always
# needs the complete, current picture of its own money.
_MAX_OTHER_ACCOUNTS_SHOWN = 20


def _max_transferable_now_paise(acc: AccountSnapshot) -> int:
    """The hard ceiling on what `acc` can send as a transfer/payment SOURCE
    right now: min(current balance, whatever's left of today's KYC/account-type
    daily allowance). Mirrors sim/core/account.py's own
    Account.can_debit()/check_daily_limit() logic (available_paise vs.
    balance — reserved_paise isn't in AccountSnapshot, so this treats the
    two as equal, which holds for every account a red-team session actually
    produces today; daily_limit_paise() with ACCOUNT_TYPE_MULTIPLIERS==0
    means no daily cap at all, balance is the only constraint).

    This exists because real sessions were observed guessing amounts blindly
    and iterating on DEBIT_REJECTED/LIMIT_EXCEEDED failures one poke at a
    time — a dozen-plus wasted steps in a row in one recorded session,
    despite balance_paise/daily_tx_volume_paise being right there in the
    prompt every turn. A human moving their own money doesn't grope for
    their own balance by trial and error; they just know it. Free-tier
    small models are unreliable at doing that arithmetic (limit * multiplier
    - already-spent, compared against balance) themselves turn after turn,
    so it's done here once, mechanically, and handed over as a ready number
    instead of raw inputs to compute from — the same "give it the answer,
    not the homework" fix _public_limits_block() already applies to the
    KYC tier table itself.
    """
    multiplier = ACCOUNT_TYPE_MULTIPLIERS.get(acc.account_type, 1)
    if multiplier == 0:
        return acc.balance_paise
    limit = KYC_DAILY_LIMITS.get(acc.kyc_level, KYC_DAILY_LIMITS[0]) * multiplier
    remaining_daily = max(0, limit - acc.daily_tx_volume_paise)
    return min(acc.balance_paise, remaining_daily)


def summarize_world_view(view: WorldView) -> str:
    lines = [f"sim_time_ns={view.sim_time_ns}"]

    # owner_id is real (== view.actor_id) only for the caller's own
    # accounts; every other account has it replaced with a pseudonymous
    # hash (WorldEngine.get_world_view) — that's exactly the signal used
    # here to split "yours" from "everyone else's".
    own = [a for a in view.accounts if a.owner_id == view.actor_id]
    other = [a for a in view.accounts if a.owner_id != view.actor_id]

    lines.append(f"YOUR accounts ({len(own)}):")
    if not own:
        lines.append("  (none yet — call create_account)")
    for acc in own:
        lines.append(
            f"  - {acc.account_id} [{acc.account_type.value}]: balance_paise={acc.balance_paise}, "
            f"status={acc.status.value}, kyc_level={acc.kyc_level}, "
            f"daily_tx_count={acc.daily_tx_count}, daily_tx_volume_paise={acc.daily_tx_volume_paise}, "
            f"max_transferable_now_paise={_max_transferable_now_paise(acc)} (hard ceiling for a transfer/"
            f"payment FROM this account this turn — balance and today's remaining daily allowance "
            f"already combined for you, don't send more than this and don't guess)"
        )

    if other:
        shown = other[:_MAX_OTHER_ACCOUNTS_SHOWN]
        lines.append(
            f"\nOTHER accounts on this branch ({len(other)} total, showing {len(shown)}) — "
            "valid as a target_account_id for transfer_funds/make_payment (pay any of these). "
            "NOT valid as source_account_id — you can only spend from an account you own:"
        )
        for acc in shown:
            lines.append(
                f"  - {acc.account_id} [{acc.account_type.value}]: balance_paise={acc.balance_paise}, "
                f"status={acc.status.value}, kyc_level={acc.kyc_level}"
            )
        if len(other) > len(shown):
            lines.append(f"  … and {len(other) - len(shown)} more not shown here.")

    return "\n".join(lines)


_POOLED_NOTE_PREFIX = "[from "


def summarize_target_notes(notes: list[str]) -> str:
    """Render the agent's own save_note history — its only durable memory
    of which accounts it has decided to target and why, since the world
    view is regenerated fresh every turn and the rolling step history
    (agents/redteam/harness.py::_HISTORY_WINDOW) is short. Unlike the
    account list this is never truncated: notes are the agent's own
    curated, presumably-already-short summary, not raw simulation state.

    Notes pooled in from an earlier session (harness.py::_pool_notes_from_branches,
    always prefixed "[from <branch_id>...]" — see there) are mixed into the
    same list as this session's own save_note calls. Without saying so
    explicitly, a continued session had no reason to treat those any
    differently from its own fresh discoveries — real sessions showed this
    plays out as literally repeating the prior session's already-committed
    move for the rest of the run (docs/redteam_agent_design.md §10's
    UNAUTHORIZED_SOURCE finding: 5 steps to find it, 10 more steps repeating
    it) rather than using the prior finding as a starting point for
    something the prior session didn't already cover. This is the
    "prompt injection" fix for that: distinguish the two kinds of note in
    the rendered block itself and instruct against re-demonstrating what's
    already proven.
    """
    if not notes:
        return (
            "Your saved target notes: (none yet — call save_note once you've picked an "
            "account to target, so you don't lose track of it across turns)"
        )
    pooled = [n for n in notes if n.startswith(_POOLED_NOTE_PREFIX)]
    own = [n for n in notes if not n.startswith(_POOLED_NOTE_PREFIX)]
    lines = []
    if pooled:
        lines.append(
            "Notes pooled in from EARLIER sessions (already investigated/demonstrated by a "
            "prior run — not something you found yourself this session):"
        )
        lines.extend(f"  - {n}" for n in pooled)
        lines.append(
            "Do not spend this session re-demonstrating one of the above from scratch — that "
            "ground is already covered. Use them as a starting point: either push the same "
            "pattern further than the prior session did (a different account type, a larger "
            "or differently-timed move, a variant that might evade the same check differently), "
            "or use the freed-up steps to probe a DIFFERENT pattern entirely (see the fraud "
            "patterns listed in your instructions). Only commit_strategy again if you've actually "
            "found or extended something beyond what's already listed here."
        )
    if own:
        if pooled:
            lines.append("\nYour OWN saved notes from this session:")
        else:
            lines.append("Your saved target notes:")
        lines.extend(f"  - {n}" for n in own)
    return "\n".join(lines)


# ToolSpec.parameter_schema is registered as an empty {} for every tool in
# sim/main.py today (real JSON Schema was never populated there) — it can't
# be relied on to tell the agent what parameters to send. This is a stopgap
# hint table for the known core + red-team tools until that's fixed upstream.
_TOOL_PARAM_HINTS: dict[str, str] = {
    "create_account": (
        "(no required parameters) — optional: initial_balance_paise (integer, default "
        "2000000 = ₹20,000), kyc_level (integer, default 0, the lowest/most-restricted tier), "
        "account_type (one of PERSONAL/MERCHANT/CASH_ENTITY/INTERNAL_SETTLEMENT/ESCROW, default PERSONAL)"
    ),
    "transfer_funds": (
        "source_account_id (required, string — must be an account you own; "
        "UNAUTHORIZED_SOURCE otherwise), target_account_id (required, string — any visible "
        "account, yours or not), amount_paise (required, a bare JSON integer — paise, not "
        "rupees, e.g. 500000, NEVER a string, and NEVER with a unit/currency suffix like "
        "'500000p' or '₹5000' attached — that fails as INVALID_PARAMETER; also check the "
        "source account's max_transferable_now_paise this turn and stay at or under it), "
        "idempotency_key (optional)"
    ),
    "make_payment": (
        "source_account_id (required, string — must be an account you own; "
        "UNAUTHORIZED_SOURCE otherwise), target_account_id (required, string — any visible "
        "account, yours or not), amount_paise (required, a bare JSON integer — paise, not "
        "rupees, e.g. 500000, NEVER a string, and NEVER with a unit/currency suffix like "
        "'500000p' or '₹5000' attached — that fails as INVALID_PARAMETER; also check the "
        "source account's max_transferable_now_paise this turn and stay at or under it), "
        "gateway_id (optional), idempotency_key (optional)"
    ),
    "inspect_account": "account_id (required, string — any account_id from your world view, yours or not)",
    "commit_strategy": "(no parameters) — call this to end the session once you've found and tested a strategy",
    "save_note": (
        "note (required, string) — freeform text, ideally naming the account_id you're "
        "targeting and your plan/observations for it. Persists for the rest of this session."
    ),
}


def summarize_tools(tools: list[ToolSpec]) -> str:
    lines = ["Available tools:"]
    for tool in tools:
        params = ", ".join(tool.parameter_schema) or _TOOL_PARAM_HINTS.get(tool.name, "(parameters unknown)")
        lines.append(f"- {tool.name}: {tool.description}. parameters: {params}")
    return "\n".join(lines)
