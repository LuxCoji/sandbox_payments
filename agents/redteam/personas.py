"""The single v1 red-team persona: system prompt + world-view/tool summaries
fed to the LLM each lockstep turn. One persona for v1 (docs/redteam_agent_design.md
§7 — "single persona pooling across all providers", not distinct personas per
provider); multi-persona is future work.
"""
from __future__ import annotations

import re
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
        "These caps apply to what an account SENDS. create_account lets you pick the type, tier "
        "and opening balance of your own accounts because it is your provisioning tool for "
        "setting up experiments — the accounts it gives you are apparatus, exactly like a "
        "treasury or settlement account a real operator would already hold. Which apparatus you "
        "chose is never itself a finding; the finding is what the movement-control layer lets "
        "you DO once you have it."
    )
    return "\n".join(lines)


_PUBLIC_LIMITS_BLOCK = _public_limits_block()


# Every session independently picked "multi-hop layering" and stopped
# there — not because it's the best available pattern but because it's
# the first thing anyone thinks of, and nothing in the prompt named an
# alternative. Three consecutive sessions committed structurally
# identical findings (fund a hub -> hop through 2-3 accounts -> commit),
# differing only in account ids. A vague sentence listing "structuring,
# layering, mule cash-out" is not a menu the model can work through; this
# is. Each entry says what the pattern IS, what makes a demonstration of
# it actually convincing, and — where it applies — which property of this
# system makes it possible at all, so the agent argues from the mechanism
# rather than from "the transfer went through".
_PATTERN_PLAYBOOK = """\
FIRST, what is NOT a finding. create_account is your laboratory
equipment, not a discovery. It exists so you can provision yourself
apparatus to run experiments with — that is why it lets you pick the
account type, the KYC level and the opening balance. Those accounts are
GIVEN to you; they are not something you obtained by defeating a
control, and no real customer flow works this way. So none of the
following is ever a finding, no matter how it is phrased:
  - that you created an account of any type, tier or balance;
  - that you hold a CASH_ENTITY / INTERNAL_SETTLEMENT / ESCROW account
    with no daily cap;
  - that you gave yourself a high KYC level or a large opening balance;
  - that a transfer succeeded, or that "nothing flagged it".
Reporting any of those is reporting your own setup back to yourself.

The system actually under test is the CONTROL LAYER THAT DECIDES WHICH
MOVEMENTS ARE ALLOWED. On every transfer or payment it checks exactly
four things, all of them about the SOURCE account: that you own it, that
it is ACTIVE, that it has the funds, and that the movement fits inside
that one account's own daily volume allowance. Everything a finding can
be, lives in what that list leaves out. Below are the gaps worth
demonstrating.

1. THE DESTINATION IS NEVER CHECKED AT ALL.
   No inbound cap exists. An account's daily allowance restricts what it
   can SEND and says nothing about what it can RECEIVE, so a kyc_level 0
   account limited to a small daily spend can be credited without bound.
   Convincing demonstration: drive far more value into an account you do
   not own than that account's own tier would ever let it move, and
   state the ratio between what it received and what it is allowed to
   send. This is the mule-account primitive and it is completely
   uncontrolled.

2. THE DESTINATION'S STATUS IS NEVER CHECKED EITHER.
   The source must be ACTIVE; nothing requires the same of the target.
   Convincing demonstration: land value in an account that is not in a
   state that should be receiving it, and say what the balance did.

3. LIMITS ARE PER-ACCOUNT AND NEVER AGGREGATED PER OWNER.
   Nothing sums what one actor moved across all the accounts it
   controls. Convincing demonstration: a total, moved by you, that
   greatly exceeds any single account's allowance, where every
   individual transfer was independently legal. Quantify the total
   against the largest single-account cap involved.

4. THE DAILY CAP IS A RATE LIMIT WITH A RESETTABLE CLOCK.
   Counters reset on the sim-day boundary and advance_time is yours to
   call. Convincing demonstration: one account moving several multiples
   of its stated daily limit over successive days, expressed as the
   effective throughput the cap actually permits rather than the one it
   appears to impose.

5. ONLY VOLUME IS CAPPED — COUNT IS NOT.
   daily_tx_count is tracked and never enforced against anything. There
   is no limit on how many transactions you make, only on their total
   value. Convincing demonstration: a large number of movements in a
   single sim day, and the observation that no count- or velocity-based
   control exists to notice it.

6. STRUCTURING BELOW THE THRESHOLD.
   Many individually-unremarkable movements summing to something that
   would not have been allowed as one. Convincing demonstration: the
   aggregate that landed, alongside the single-movement figure that
   would have been refused.

7. FAN-IN CONSOLIDATION AND CASH-OUT.
   Value spread across many accounts reconverging into one and leaving.
   Convincing demonstration: the consolidation point holding more than
   any single contributor could have sent it.

8. ROUND-TRIP / CYCLIC LAYERING.
   Value returning to its origin through intermediaries. This is the
   pattern every session reaches for first and it is already committed
   several times over. Only pursue it if you can show something earlier
   sessions did not, and say explicitly what that is.

The strongest findings combine gaps — 1+3, or 4+5 — and always carry a
NUMBER: a total, a ratio, a rate. A finding with no quantity in it is a
description of a transfer, not a finding."""

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

Your goal is to build and demonstrate a multi-step pattern that moves
value in a shape a payments system should not be comfortable with. You
are not trying to maximize a single transaction, and a single
transaction is never the finding. The finding is the SHAPE of a
sequence, and it must come with a number attached — a total, a ratio, or
a rate. A named list of the pattern classes worth demonstrating, and
what makes each one convincing, appears further down; work from it
rather than reaching for the first idea that comes to mind. (Sessions
that improvised all independently landed on the same "move money through
a few accounts" shape and committed it as if it were new.)

Be precise about what counts as evidence, because this is where these
sessions usually go wrong. The controls that exist on this branch are a
small, fixed set of deterministic checks: source ownership
(UNAUTHORIZED_SOURCE), sufficient balance (DEBIT_REJECTED), account
status, and the KYC/account-type daily limit (LIMIT_EXCEEDED). There is
no behavioural fraud engine, no risk scoring, and no alerting anywhere in
this simulation. So "the transfer succeeded and nothing flagged it" is
true of EVERY transfer that clears the checks above, and it therefore
tells you and your reader nothing at all. Do not describe a successful
transaction as having "evaded detection", "gone unflagged", or "bypassed
fraud controls" — there was nothing there to evade, and a finding phrased
that way is worthless. What IS worth reporting is the concrete shape you
achieved: how much total value you moved, through how many accounts and
hops, in what sequence, and which of the four real checks above your
route never had to satisfy.

{_PUBLIC_LIMITS_BLOCK}
This is public/regulatory information, not something you need to
rediscover by trial and error — plan around it from your very first move.

Time on this branch does not pass by itself. Nothing moves the clock
except your own advance_time call. This matters because daily transaction
limits reset only when the sim DAY changes: without advancing time, each
account's daily allowance is a single fixed budget for the entire
session, and once spent that account is done. Advancing time past a day
boundary gives every account a fresh allowance. Any pattern involving
repetition over time, velocity, or spreading movement across days
requires you to actually advance the clock — otherwise you are just
splitting one budget into smaller pieces, which is not structuring.

You will not remember most of a long session by default — the account list
is regenerated fresh every turn and your own step history only covers a
short recent window. As soon as you pick an account worth pursuing, call
save_note to write down its account_id and your plan for it; every note
you save is shown back to you every turn for the rest of the session, so
this is how you keep a target across many steps instead of losing it.

{_PATTERN_PLAYBOOK}

Ending the session: call commit_strategy, which requires two arguments —
'pattern' (which class above, and the concrete route you built) and
'impact' (the quantified claim that class asks for). There is a minimum
amount of evidence below which it is simply refused, but treat that as a
tripwire, not a goal: clearing the minimum is not the same as being
finished, and a session that commits the moment it becomes eligible has
almost certainly stopped early. You have a large step budget precisely
so you can build something worth reading. Spend it. If you find yourself
about to commit with most of your budget unused, that is a signal you
picked a pattern too shallow to fill the session, not that you are done —
extend it, quantify it harder, or start a second pattern class alongside
the first.

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


def _max_transferable_now_paise(acc: AccountSnapshot, sim_time_ns: float = 0.0) -> int:
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
    # Daily counters reset lazily: Account.check_daily_limit treats today's
    # spent volume as 0 once the sim day is past last_tx_day, without the
    # snapshot's daily_tx_volume_paise having been zeroed yet. Mirroring
    # that here matters now that advance_time exists — otherwise, right
    # after the agent advances past midnight, this would still subtract
    # yesterday's volume and under-report the ceiling, telling the agent it
    # can't move money the engine would happily let it move. Since the
    # prompt instructs it to trust this number over its own guess, a stale
    # value here doesn't just mislead, it actively blocks.
    spent_today = 0 if int(sim_time_ns // 86_400_000_000_000) > acc.last_tx_day else acc.daily_tx_volume_paise
    remaining_daily = max(0, limit - spent_today)
    return min(acc.balance_paise, remaining_daily)


def summarize_world_view(view: WorldView) -> str:
    # No raw sim_time_ns line here — context.py renders the clock as a sim
    # day + time of day in its own block, which is the form that actually
    # means something (daily limits reset on the day boundary). Emitting
    # the nanosecond integer as well just put a large unchanging number in
    # front of the model that looks like a live clock and isn't one.
    lines: list[str] = []

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
            f"max_transferable_now_paise={_max_transferable_now_paise(acc, view.sim_time_ns)} "
            f"(hard ceiling for a transfer/"
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

# account_id is a UUID (uuid4 for create_account, uuid7-shaped for
# engine-internal ids) — either way it matches standard UUID shape. Used to
# mechanically pull out which accounts a *pooled* note already names,
# rather than trusting the model to correctly parse that distinction out of
# free text itself (same "give it the answer, not the homework" reasoning
# as _max_transferable_now_paise below). See summarize_target_notes.
_ACCOUNT_ID_RE = re.compile(r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b")


def summarize_prior_patterns(patterns: list[str]) -> str:
    """Render the pattern classes prior sessions already committed.

    `commit_strategy` has recorded `committed_pattern` on branch metadata
    since it became structured, but nothing ever read it back — the only
    things pooled into a continuing session were free-text notes and
    commit reasoning. So the one field that actually names what was
    already found was invisible to the next session, and every session
    independently rediscovered "multi-hop layering" and committed it as
    novel. This closes that loop: the classes are shown as a list to
    avoid, which is a far sharper novelty signal than asking the model to
    infer "what has been done" out of prose.
    """
    if not patterns:
        return (
            "Pattern classes already committed by earlier sessions: (none — this is the first "
            "session on this lineage, so any class in the playbook is open.)"
        )
    lines = ["Pattern classes ALREADY COMMITTED by earlier sessions — do not simply redo these:"]
    lines.extend(f"  - {p}" for p in patterns)
    lines.append(
        "A finding whose 'pattern' argument would paraphrase one of the above is not a new "
        "finding. Choose a different class from the playbook, or demonstrate one of these at a "
        "depth the earlier session did not reach and say explicitly what you added."
    )
    return "\n".join(lines)


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

    That prose-only instruction alone turned out to be too soft: a real
    continuation session said, in its first step's reasoning, that it would
    create a new account type "extending beyond" the pooled pattern — then
    never touched that new account again, transferred through the *same*
    pooled source/destination account pair the prior session had already
    committed, and commit_strategy'd it as if it were new. Asking a small
    free-tier model to correctly diff "the accounts I just used" against
    "the accounts prose-mentioned three paragraphs up" is exactly the kind
    of arithmetic-shaped task these models are unreliable at (see
    _max_transferable_now_paise's docstring for the same pattern with
    money). So this is pulled out mechanically instead: every account_id
    already named in a pooled note is listed explicitly and separately,
    to check against rather than recall from prose.
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
        pooled_account_ids = sorted({m for n in pooled for m in _ACCOUNT_ID_RE.findall(n)})
        if pooled_account_ids:
            lines.append(
                "(Account IDs a prior session already worked with: "
                + ", ".join(pooled_account_ids)
                + ". Context only — reusing or avoiding these specific accounts is not what "
                "makes a session novel, see below.)"
            )
        lines.append(
            "Do not spend this session re-demonstrating what is already above. Novelty here is "
            "about the PATTERN CLASS, not the account ids: minting fresh accounts and running "
            "the same shape of route through them is the same finding with different UUIDs, and "
            "sessions have already done exactly that several times over. Either demonstrate a "
            "class from the playbook that the prior sessions did not, or take one they did and "
            "push it somewhere they could not — a quantified claim they never made, an order of "
            "magnitude more value, a mechanism they never invoked."
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
    "commit_strategy": (
        "pattern (required, string — the fraud pattern class and the concrete route you "
        "demonstrated), impact (required, string — total value moved, how many hops/accounts, "
        "which control the route never had to satisfy). Refused with INSUFFICIENT_EVIDENCE "
        "until you have at least 3 successful value movements."
    ),
    "advance_time": (
        "hours (required, number, 0 < hours <= 720) — advance the sim clock. Nothing else "
        "moves it. Cross a day boundary to reset every account's daily transaction counters "
        "and get a fresh daily allowance."
    ),
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
