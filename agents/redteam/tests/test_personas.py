"""Unit tests for personas.py's prompt-assembly helpers."""
from __future__ import annotations

import uuid

from agents.redteam.personas import (
    _max_transferable_now_paise,
    summarize_prior_patterns,
    summarize_target_notes,
    summarize_world_view,
)
from sim.core.interfaces import AccountSnapshot, AccountStatus, AccountType, ActorRole, GlobalParams, WorldView

# NOTE: uuid.uuid4() here, not sim.*'s uuid5 determinism rule (CLAUDE.md) —
# that rule is scoped to sim/ for state-hash reproducibility; this is test
# fixture data in agents/redteam/tests/, generated fresh per test run
# specifically so the assertions can't be mistaken for depending on any
# particular account_id's shape or digits, only on "is a UUID".


def _account(
    account_id: str = "acc-1",
    owner_id: str = "actor-1",
    account_type: AccountType = AccountType.PERSONAL,
    balance_paise: int = 100_00_00,
    kyc_level: int = 0,
    daily_tx_volume_paise: int = 0,
) -> AccountSnapshot:
    return AccountSnapshot(
        account_id=account_id, account_type=account_type, balance_paise=balance_paise,
        status=AccountStatus.ACTIVE, kyc_level=kyc_level, created_at=0.0, daily_tx_count=0,
        daily_tx_volume_paise=daily_tx_volume_paise, linked_device_ids=(), owner_id=owner_id,
    )


def test_max_transferable_now_paise_capped_by_daily_limit_not_balance() -> None:
    # kyc_level 0 PERSONAL: daily limit = 10_000_00 (₹10K). Balance is far
    # above that, so the daily allowance (minus what's already spent today)
    # is the binding constraint, not the balance.
    acc = _account(account_type=AccountType.PERSONAL, balance_paise=50_00_000, kyc_level=0,
                    daily_tx_volume_paise=8_000_00)
    assert _max_transferable_now_paise(acc) == 2_000_00  # 10_000_00 - 8_000_00 remaining


def test_max_transferable_now_paise_capped_by_balance_not_daily_limit() -> None:
    # High KYC tier / MERCHANT multiplier gives a huge daily allowance;
    # balance is the actual binding constraint here.
    acc = _account(account_type=AccountType.MERCHANT, balance_paise=5_000_00, kyc_level=3)
    assert _max_transferable_now_paise(acc) == 5_000_00


def test_max_transferable_now_paise_no_daily_limit_account_type() -> None:
    # CASH_ENTITY has ACCOUNT_TYPE_MULTIPLIERS == 0 -> no daily cap at all,
    # per docs/redteam_agent_design.md §12 — balance is the only ceiling.
    acc = _account(account_type=AccountType.CASH_ENTITY, balance_paise=999_00_00_00)
    assert _max_transferable_now_paise(acc) == 999_00_00_00


def test_summarize_world_view_shows_precomputed_ceiling_for_own_accounts() -> None:
    """Real sessions were observed guessing transfer amounts blindly and
    iterating on DEBIT_REJECTED/LIMIT_EXCEEDED one poke at a time despite
    balance_paise being in the prompt every turn — free-tier models aren't
    reliable at doing the balance-vs-daily-allowance arithmetic themselves.
    The fix hands over the already-computed ceiling instead of raw inputs.
    """
    view = WorldView(
        actor_id="actor-1", actor_role=ActorRole.RED_AGENT, sim_time_ns=0.0,
        accounts=(_account(balance_paise=5_00_000, kyc_level=0),), merchants=(), devices=(),
        global_params=GlobalParams(fee_schedules=(), rail_limits=(), settlement_cut_off_ns=0.0),
    )
    rendered = summarize_world_view(view)
    assert "max_transferable_now_paise=500000" in rendered  # min(balance=500000, daily allowance=1000000)
    assert "don't send more than this and don't guess" in rendered


def test_summarize_target_notes_empty() -> None:
    assert "none yet" in summarize_target_notes([])


def test_summarize_target_notes_own_only() -> None:
    rendered = summarize_target_notes(["acc-123: looks structurable"])
    assert "acc-123: looks structurable" in rendered
    assert "pooled" not in rendered.lower()


def test_summarize_target_notes_distinguishes_pooled_from_own() -> None:
    """Pooled notes (harness.py::_pool_notes_from_branches, always prefixed
    "[from <branch_id>...]") must be called out as already-investigated by a
    prior session, with an explicit instruction not to just re-demonstrate
    them — the fix for a continued session otherwise having no reason to
    treat them differently from its own fresh discoveries (real sessions
    were observed just repeating the pooled finding for the rest of the
    run, docs/redteam_agent_design.md §10).
    """
    notes = [
        "[from red-team/session-1] acc-42: unauthorized-source drain confirmed",
        "acc-99: trying structuring under the kyc_level 1 threshold",
    ]
    rendered = summarize_target_notes(notes)

    assert "EARLIER sessions" in rendered
    assert "acc-42: unauthorized-source drain confirmed" in rendered
    assert "do not spend this session re-demonstrating" in rendered.lower()
    assert "Your OWN saved notes from this session" in rendered
    assert "acc-99: trying structuring under the kyc_level 1 threshold" in rendered


def test_summarize_target_notes_pooled_only() -> None:
    notes = ["[from red-team/session-1, prior session's commit_strategy] found X"]
    rendered = summarize_target_notes(notes)
    assert "found X" in rendered
    assert "Your OWN saved notes" not in rendered


def test_summarize_target_notes_frames_novelty_as_pattern_not_account_ids() -> None:
    """Pooled account ids are surfaced as context, but must NOT be presented
    as the novelty test.

    An earlier version told the agent to check whether its flow involved an
    account_id absent from the pooled list. Sessions then optimised for
    exactly that: minting fresh accounts and re-running the identical route
    through them, with reasoning like "using unlisted accounts distinct
    from prior session notes". Same finding, new UUIDs. Novelty has to be
    about the pattern class, so the ids stay (they are useful) but the
    instruction attached to them changed.
    """
    cash_entity_id = str(uuid.uuid4())
    personal_id = str(uuid.uuid4())
    notes = [
        f"[from red-team/session-1, prior session's commit_strategy] routed funds through "
        f"CASH_ENTITY account {cash_entity_id} to low-KYC personal account {personal_id}",
    ]
    rendered = summarize_target_notes(notes)
    assert cash_entity_id in rendered
    assert personal_id in rendered
    assert "not what" in rendered and "makes a session novel" in rendered
    assert "PATTERN CLASS" in rendered
    assert "same finding with different UUIDs" in rendered


def test_summarize_prior_patterns_lists_committed_classes() -> None:
    """commit_strategy has recorded committed_pattern on branch metadata
    since it became structured, but nothing read it back — so every session
    independently rediscovered "multi-hop layering" and committed it as
    novel. This is the block that closes that loop.
    """
    assert "none" in summarize_prior_patterns([]).lower()

    rendered = summarize_prior_patterns(["[red-team/s1] cyclic layering (claimed impact: 2.5M paise)"])
    assert "ALREADY COMMITTED" in rendered
    assert "cyclic layering" in rendered
    assert "would paraphrase one of the above is not a new" in rendered
