"""What happens after a human decides: freeze, clear, or ask for more.

**Nothing here freezes an account on a model's say-so.** The wire rail runs at
roughly 12% precision, so an automatic freeze would stop about eight innocent
parties for every real laundering operation. Every freeze requires a named
reviewer and records who they were.

Three rules, each because the alternative is a real failure:

**A freeze is a request, not an act.** `request_freeze` records an intent and
returns it; nothing here touches an account. The system that actually holds
funds is the simulation engine, and it should receive an instruction with a case
behind it rather than a callback from a model.

**Every action is appended, never overwritten.** A cleared case that turns out to
be laundering has to be reconstructible - what was decided, by whom, on what
evidence, and when. That is the difference between an audit trail and a status
column.

**Clearing a case does not delete it.** The rail flagged it for a stated reason.
A cleared case stays in the log and stays available to a later review, because a
reviewer's judgement is one piece of evidence rather than the last word.

There is a fourth rule that belongs in the workflow rather than in code: **do not
tell the customer.** Warning someone that they are under money-laundering review
is "tipping off" - a criminal offence under India's PMLA, the US Bank Secrecy Act
and the UK's Proceeds of Crime Act. Card fraud is the opposite: a step-up
challenge is expected and should say what it is. `notify_customer` enforces the
distinction.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path


class Action(str, Enum):
    FREEZE_REQUESTED = "freeze_requested"
    CLEARED = "cleared"
    INFO_REQUESTED = "info_requested"
    REOPENED = "reopened"


# A freeze on a case worth more than this needs a second reviewer. Large freezes
# are both the most damaging when wrong and the most likely to be challenged, so
# they should not rest on one person's judgement. One crore, in paise.
DUAL_APPROVAL_ABOVE_PAISE = 1_000_000_000


@dataclass
class Decision:
    """One human action on one case. Appended to the log, never edited."""

    case_id: str
    rail: str
    action: str
    reviewer: str
    reason: str
    at: str
    case_value_paise: int = 0
    evidence: str = ""
    model_score: float = 0.0
    second_reviewer: str | None = None
    # Set when a freeze needs a second approval it has not yet received.
    pending: bool = False

    def to_dict(self) -> dict:
        return asdict(self)


class CaseLog:
    """Append-only record of every decision.

    JSON Lines rather than a table: one line per decision, appended, never
    rewritten. A file that can only grow is hard to corrupt and trivial to
    audit, and this volume - a few hundred decisions a day - needs nothing more.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def append(self, decision: Decision) -> Decision:
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(decision.to_dict()) + "\n")
        return decision

    def all(self) -> list[Decision]:
        if not self.path.exists():
            return []
        return [Decision(**json.loads(line))
                for line in self.path.read_text(encoding="utf-8").splitlines()
                if line.strip()]

    def for_case(self, case_id: str) -> list[Decision]:
        return [d for d in self.all() if d.case_id == case_id]

    def current_state(self, case_id: str) -> str:
        """The latest action on a case, or "open" if there has been none.

        Derived from the log rather than stored separately, so the two can never
        disagree.
        """
        history = self.for_case(case_id)
        return history[-1].action if history else "open"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def request_freeze(case, reviewer: str, reason: str, log: CaseLog,
                   second_reviewer: str | None = None) -> Decision:
    """Record a request to freeze the accounts in a case.

    Returns the recorded decision. **It does not freeze anything** - the engine
    holds the funds, and it should act on an instruction with a case behind it
    rather than on a callback from a model.

    A reviewer is required. Passing an empty one raises rather than defaulting
    to "system", because "who decided this" is the question an audit asks first.
    """
    if not reviewer.strip():
        raise ValueError("a freeze needs a named reviewer")
    if not reason.strip():
        raise ValueError("a freeze needs a stated reason")

    value = int(getattr(case, "amount_paise", 0))
    needs_second = value > DUAL_APPROVAL_ABOVE_PAISE and not second_reviewer

    return log.append(Decision(
        case_id=str(case.tx_id), rail=str(getattr(case, "rail", "wire")),
        action=Action.FREEZE_REQUESTED.value,
        reviewer=reviewer.strip(), reason=reason.strip(), at=_now(),
        case_value_paise=value, evidence=str(getattr(case, "reason", "")),
        model_score=float(getattr(case, "score", 0.0)),
        second_reviewer=second_reviewer, pending=needs_second,
    ))


def clear_case(case, reviewer: str, reason: str, log: CaseLog) -> Decision:
    """Record that a reviewer judged a case not to be fraud.

    The case is not deleted. It stays in the log and can be reopened - the rail
    flagged it for a stated reason, and a reviewer's judgement is one piece of
    evidence rather than the last word.
    """
    if not reviewer.strip():
        raise ValueError("clearing a case needs a named reviewer")

    return log.append(Decision(
        case_id=str(case.tx_id), rail=str(getattr(case, "rail", "wire")),
        action=Action.CLEARED.value, reviewer=reviewer.strip(),
        reason=reason.strip() or "no reason given", at=_now(),
        case_value_paise=int(getattr(case, "amount_paise", 0)),
        evidence=str(getattr(case, "reason", "")),
        model_score=float(getattr(case, "score", 0.0)),
    ))


def request_information(case, reviewer: str, question: str,
                        log: CaseLog) -> Decision:
    """Record that a reviewer needs more before deciding.

    A distinct state from open, because a case waiting on someone else should
    not keep resurfacing at the top of the queue.
    """
    return log.append(Decision(
        case_id=str(case.tx_id), rail=str(getattr(case, "rail", "wire")),
        action=Action.INFO_REQUESTED.value, reviewer=reviewer.strip(),
        reason=question.strip(), at=_now(),
        case_value_paise=int(getattr(case, "amount_paise", 0)),
        evidence=str(getattr(case, "reason", "")),
        model_score=float(getattr(case, "score", 0.0)),
    ))


def reopen_case(case_id: str, reviewer: str, reason: str,
                log: CaseLog) -> Decision:
    """Reopen a cleared case. New information, or a later pattern, may change it."""
    return log.append(Decision(
        case_id=str(case_id), rail="wire", action=Action.REOPENED.value,
        reviewer=reviewer.strip(), reason=reason.strip(), at=_now(),
    ))


def notify_customer(rail: str, action: str) -> dict:
    """May the customer be told? On the wire rail, no.

    Warning someone that they are under money-laundering review is **tipping
    off** - a criminal offence under India's PMLA, the US Bank Secrecy Act and
    the UK's Proceeds of Crime Act. The bank files a report to the regulator and
    says nothing to the customer.

    Card fraud is the opposite. A step-up challenge is expected, and a customer
    who is not told why their payment was declined will simply call support.
    """
    if rail == "wire":
        return {"notify": False,
                "reason": "tipping off - telling a customer they are under "
                          "AML review is a criminal offence",
                "instead": "file a suspicious activity report to the regulator"}

    messages = {
        "STEP_UP": "Confirm this payment with the code we just sent you.",
        "BLOCK": "We stopped this payment because it looked unusual. "
                 "Call the number on your card if it was you.",
    }
    return {"notify": action in messages, "message": messages.get(action, ""),
            "reason": "card-fraud contact is expected and reduces support load"}


def open_cases(cases, log: CaseLog) -> list:
    """The queue with decided cases removed.

    Cleared and frozen cases drop out; a case awaiting information drops out
    until it is answered. Reopened cases return. Derived from the log each time
    rather than stored, so the queue cannot drift from the record.
    """
    settled = {Action.CLEARED.value, Action.FREEZE_REQUESTED.value,
               Action.INFO_REQUESTED.value}
    return [c for c in cases if log.current_state(str(c.tx_id)) not in settled]


def summarise(log: CaseLog) -> dict:
    """What the reviewers have done - for the console and for an auditor."""
    decisions = log.all()
    by_action: dict[str, int] = {}
    for decision in decisions:
        by_action[decision.action] = by_action.get(decision.action, 0) + 1

    frozen = [d for d in decisions if d.action == Action.FREEZE_REQUESTED.value]
    return {
        "total_decisions": len(decisions),
        "by_action": by_action,
        "value_frozen_paise": sum(d.case_value_paise for d in frozen),
        "awaiting_second_approval": sum(1 for d in frozen if d.pending),
        "reviewers": sorted({d.reviewer for d in decisions if d.reviewer}),
    }
