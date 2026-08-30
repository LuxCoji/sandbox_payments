"""Unit tests for personas.py's prompt-assembly helpers."""
from __future__ import annotations

from agents.redteam.personas import _max_transferable_now_paise, summarize_target_notes, summarize_world_view
from sim.core.interfaces import AccountSnapshot, AccountStatus, AccountType, ActorRole, GlobalParams, WorldView


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
