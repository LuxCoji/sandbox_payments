"""Single owner of the red-team agent's per-turn LLM context.

Context assembly used to be split across two modules with no one place
responsible for it: `harness.py` concatenated a `world_summary` string,
then `llm_router.decide_next_action()` separately decided the message
layout and interleaved the history/outcome blocks around it. That split
is why the context grew the defects this module exists to fix — nobody
owned the whole picture, so each addition got appended wherever it was
easiest.

Everything the model sees on a turn is now built here, from a
`TurnContext`, in one ordered place. `personas.py` still owns the static
persona prompt and the individual world-view/notes/tools *renderers*;
this module owns which blocks exist, what goes in them, and what order
they appear in.

Fixes, each traceable to observed session behavior:

1. **Step budget.** `session_max_steps` was never shown to the model —
   not once, anywhere. With no idea whether it had 3 steps or 300, the
   rational move after any success was to bank it, which is exactly what
   sessions did (commit at step 3-6 of 30). Now every turn states the
   step number, the remaining budget, and a phase-appropriate directive.

2. **Cumulative evidence ledger.** The only memory of prior steps was a
   12-step rolling window that dropped off, plus agent-initiated notes.
   So at step 20 the agent had no way to state what it had actually
   moved in total — and a fraud pattern is a claim about an aggregate
   (N movements, M accounts, total value). Without the aggregate, every
   claim was necessarily about the last thing that happened, which is
   why commits described a single transfer. The ledger accumulates every
   successful value movement for the whole session and never truncates.

3. **A real clock.** `sim_time_ns` was rendered as a raw nanosecond
   integer that never changed, which reads like a live clock and isn't
   one. Now rendered as a sim day + time of day, with the frozen-clock
   fact stated explicitly and `advance_time` named as the only thing
   that moves it.

4. **Repeat-failure aggregation.** The model saw 12 individual history
   lines and was asked to notice it was repeating itself. It didn't —
   one session spent 15 consecutive steps re-attempting the same
   rejected transfer. Identical (tool, source, target) attempts that
   already failed are now counted and surfaced as their own block.

5. **Intent continuity.** History lines recorded what was done but not
   why, so the agent's own strategy evaporated every turn and it
   re-derived intent from scratch. History lines now carry a compressed
   reasoning tail.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from agents.redteam.personas import (
    summarize_prior_patterns,
    summarize_target_notes,
    summarize_world_view,
)

if TYPE_CHECKING:
    from sim.core.interfaces import WorldView

NANOS_PER_DAY = 86_400_000_000_000
NANOS_PER_HOUR = 3_600_000_000_000

# Tools that actually move value. Only these count as evidence — an
# inspect_account or a save_note is not something you demonstrated, and
# counting them was part of how a session talked itself into believing it
# had built a case out of six steps of looking around.
_VALUE_MOVEMENT_TOOLS = frozenset({"transfer_funds", "make_payment"})

# How many past steps appear in the rolling detail window. Distinct from
# the evidence ledger, which is cumulative and never truncated: this
# window is for "what did I just try, including failures", the ledger is
# for "what have I established".
HISTORY_WINDOW = 12

# A (tool, source, target) triple that has already failed this many times
# gets called out explicitly rather than left for the model to notice
# among the history lines.
_REPEAT_FAILURE_THRESHOLD = 2


@dataclass(frozen=True)
class StepRecord:
    """One completed step. The harness appends these; everything else in
    this module is derived from the list of them."""

    step: int
    tool_name: str
    parameters: dict[str, object]
    reasoning: str
    success: bool
    error_code: str | None = None
    error_message: str | None = None

    @property
    def is_value_movement(self) -> bool:
        return self.success and self.tool_name in _VALUE_MOVEMENT_TOOLS

    def signature(self) -> tuple[str, object, object]:
        """Identity for repeat-attempt detection — the tool plus the
        accounts involved, deliberately ignoring amount: re-sending the
        same route with a slightly different number is the exact loop
        this is meant to catch, so varying the amount must not make an
        attempt look new."""
        return (
            self.tool_name,
            self.parameters.get("source_account_id"),
            self.parameters.get("target_account_id") or self.parameters.get("account_id"),
        )


@dataclass
class SessionMemory:
    """Everything that outlives a single turn."""

    notes: list[str] = field(default_factory=list)
    steps: list[StepRecord] = field(default_factory=list)
    # Pattern CLASSES earlier sessions already committed, pooled from
    # their branch metadata (harness._pool_notes_from_branches). Distinct
    # from `notes`: notes are free text about accounts, this is the list
    # of findings that already exist, and it is what makes "is this
    # session novel" answerable without the model having to infer it from
    # prose. Read-only for the session — nothing appends here at runtime.
    prior_patterns: list[str] = field(default_factory=list)

    def record(self, step: StepRecord) -> None:
        self.steps.append(step)

    @property
    def value_movements(self) -> list[StepRecord]:
        return [s for s in self.steps if s.is_value_movement]


def _paise(amount: object) -> int:
    try:
        return int(str(amount))
    except (TypeError, ValueError):
        return 0


def format_sim_clock(sim_time_ns: float) -> str:
    """Render the clock as something with meaning attached to it.

    A raw `sim_time_ns=1749600000000000` tells the model nothing it can
    act on, and worse, looks like a running clock. What actually matters
    is the sim *day*, because that's the unit daily transaction limits
    reset on (Account.check_daily_limit compares
    sim_time_ns // NANOS_PER_DAY against the account's last_tx_day).
    """
    day = int(sim_time_ns // NANOS_PER_DAY)
    into_day_ns = sim_time_ns - day * NANOS_PER_DAY
    hours = int(into_day_ns // NANOS_PER_HOUR)
    minutes = int((into_day_ns % NANOS_PER_HOUR) // (NANOS_PER_HOUR // 60))
    return f"sim day {day}, {hours:02d}:{minutes:02d}"


def render_budget(step: int, max_steps: int, movements: int) -> str:
    """The step budget, plus what to be doing with it right now.

    Sessions committed at step 3-6 of 30 because nothing ever told them
    how much room they had. Stating the remaining budget is most of the
    fix; naming the current phase's job is the rest, since "you have 24
    steps left" without "so don't bank a one-transfer finding yet" still
    leaves banking-early as a defensible read.
    """
    remaining = max_steps - step + 1
    progress = step / max_steps if max_steps else 1.0
    if progress <= 0.25:
        phase = (
            "EARLY. Set up and look around. Do not commit anything yet — you have barely "
            "started and a finding built this early is necessarily a single transaction, "
            "which is not a pattern."
        )
    elif progress <= 0.7:
        phase = (
            "MIDDLE. This is where the actual work happens: execute the multi-step pattern "
            "you are building — more hops, more accounts, repeated movement, crossing a day "
            "boundary. Accumulate movements you can point at later."
        )
    elif progress <= 0.9:
        phase = (
            "LATE. Start consolidating. Make sure what you have is a *pattern* (a sequence "
            "with a shape) and not a list of unrelated transfers, and fill any gap that "
            "would make it unconvincing."
        )
    else:
        phase = (
            "FINAL STEPS. Commit now if you have a real pattern — if you run out of steps "
            "without calling commit_strategy, nothing you did gets recorded as a finding."
        )
    return (
        f"Step {step} of {max_steps} — {remaining} step(s) remaining. {phase}\n"
        f"Successful value movements so far: {movements}."
    )


def render_evidence(memory: SessionMemory) -> str:
    """The cumulative case the agent has actually built.

    This is the block that did not exist at all before. Its absence is
    the reason commits described whatever had just happened: the rolling
    12-step window was the only record, so by construction the agent
    could only argue about recent events, and "I transferred money and
    it worked" was a complete and accurate summary of everything it
    could still see.
    """
    movements = memory.value_movements
    if not movements:
        return (
            "Evidence you have accumulated: (nothing yet — no successful transfer or payment "
            "so far this session. You cannot commit a finding until you have actually moved "
            "value at least 3 times.)"
        )

    lines = ["Evidence you have accumulated (every successful value movement this session):"]
    total = 0
    sources: set[str] = set()
    targets: set[str] = set()
    for rec in movements:
        amount = _paise(rec.parameters.get("amount_paise"))
        src = str(rec.parameters.get("source_account_id", "?"))
        dst = str(rec.parameters.get("target_account_id", "?"))
        total += amount
        sources.add(src)
        targets.add(dst)
        lines.append(f"  - step #{rec.step}: {rec.tool_name} {amount}p  {src} -> {dst}")
    lines.append(
        f"Totals: {len(movements)} movement(s), {total}p moved, "
        f"{len(sources)} distinct source account(s), {len(targets)} distinct destination(s)."
    )
    lines.append(
        "These totals are the raw material for a finding — a pattern is a claim about this "
        "aggregate (how much, through how many accounts, in what shape), not about any one line."
    )
    return "\n".join(lines)


def render_history(memory: SessionMemory) -> str:
    """Recent steps in detail, including failures and the reasoning that
    motivated each — the reasoning tail is what keeps the agent's own
    strategy alive across turns instead of being re-derived from the
    world state every time."""
    recent = memory.steps[-HISTORY_WINDOW:]
    if not recent:
        return "Recent steps: (none yet — this is your first turn)"

    lines = [f"Recent steps (last {len(recent)} of {len(memory.steps)}), oldest first:"]
    for rec in recent:
        detail = ""
        if rec.tool_name in _VALUE_MOVEMENT_TOOLS:
            detail = (
                f" [{rec.parameters.get('source_account_id')} -> "
                f"{rec.parameters.get('target_account_id')}, "
                f"{rec.parameters.get('amount_paise')}p]"
            )
        elif rec.tool_name == "inspect_account":
            detail = f" [{rec.parameters.get('account_id')}]"
        elif rec.tool_name == "advance_time":
            detail = f" [+{rec.parameters.get('hours')}h]"
        status = "OK" if rec.success else f"FAILED ({rec.error_code}) {rec.error_message}"
        why = f' — intent: "{rec.reasoning[:120]}"' if rec.reasoning else ""
        lines.append(f"  #{rec.step} {rec.tool_name}{detail}: {status}{why}")
    return "\n".join(lines)


def render_repeat_failures(memory: SessionMemory) -> str:
    """Attempts that have already failed more than once, aggregated.

    Telling the model "do not blindly repeat a FAILED action" and handing
    it twelve individual lines asks it to do a diff it demonstrably does
    not do — one session spent fifteen consecutive steps re-attempting
    the same rejected transfer with slightly different amounts. Counting
    them here turns that diff into a fact it just has to read.
    """
    counts: dict[tuple[str, object, object], list[StepRecord]] = {}
    for rec in memory.steps:
        if rec.success:
            continue
        counts.setdefault(rec.signature(), []).append(rec)

    repeated = {sig: recs for sig, recs in counts.items() if len(recs) >= _REPEAT_FAILURE_THRESHOLD}
    if not repeated:
        return ""

    lines = ["STOP REPEATING THESE — each has already failed more than once:"]
    for (tool, src, dst), recs in repeated.items():
        steps = ", ".join(f"#{r.step}" for r in recs)
        lines.append(
            f"  - {tool} {src} -> {dst}: failed {len(recs)}x (steps {steps}), "
            f"last error {recs[-1].error_code}: {recs[-1].error_message}"
        )
    lines.append(
        "Retrying one of these with a different amount is still the same attempt and will "
        "fail the same way. Change the route, the source account, or the tool — or advance "
        "time if what you hit was a daily limit."
    )
    return "\n".join(lines)


@dataclass
class TurnContext:
    """Everything the model sees on one turn, before rendering."""

    step: int
    max_steps: int
    view: WorldView
    tools_block: str
    memory: SessionMemory
    last_outcome: str | None = None

    def render(self) -> str:
        """Assemble the user message.

        Order is deliberate: stable//situational blocks first, then what
        the agent has built, then what just happened. The most
        behaviour-shaping blocks (budget, evidence, repeat-failures) are
        given their own headers rather than being buried inside a
        "current world view" blob the way the tool list used to be.
        """
        blocks = [
            f"## Your budget\n{render_budget(self.step, self.max_steps, len(self.memory.value_movements))}",
            f"## Tools\n{self.tools_block}",
            f"## Clock\n{format_sim_clock(self.view.sim_time_ns)}. "
            "The clock is frozen unless you call advance_time — nothing else on this branch "
            "moves it. Daily transaction limits only reset when the sim day changes, so "
            "advance_time is the only way to get a fresh daily allowance on any account.",
            f"## World state\n{summarize_world_view(self.view)}",
            f"## {render_evidence(self.memory)}",
            f"## Prior findings\n{summarize_prior_patterns(self.memory.prior_patterns)}",
            f"## Notes\n{summarize_target_notes(self.memory.notes)}",
            f"## {render_history(self.memory)}",
        ]
        repeats = render_repeat_failures(self.memory)
        if repeats:
            blocks.append(f"## {repeats}")
        if self.last_outcome:
            blocks.append(f"## Last action\n{self.last_outcome}")
        blocks.append("Decide your next single action.")
        return "\n\n".join(blocks)
