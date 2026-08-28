# Red-Team Agents — Design Notes

Status: proposal, not yet implemented. Scope: red-team only (blue-team deferred).
Builds on `sim/gateway`, `sim/chrono`. Companion to `docs/chrono_dag.md` and
`docs/interfaces.md` — read those first for the mechanics this proposes to use.

## Where this sits today

The type system already anticipated this: `ActorRole.RED_AGENT` and a capability
grant for it exist in `sim/gateway/interfaces.py`, and `sim/gateway/adapters.py`
ships an empty `LangGraphAdapter` — `as_tool_node()` just raises
`NotImplementedError`. Nothing spawns a red agent, nothing calls an LLM, and no
code builds a `RED_AGENT` `ActorContext` and hands it to anything. That last part
is step zero — before any of the below runs, something has to construct a
persistent actor identity and context for the agent and get it a `WorldView`
through the normal `WorldEngine.get_world_view(actor_id, actor_role)` path.

Three decisions are tangled together — **sim-time vs. API-call time**, **when
adversarial agents start acting**, and **whether branching is ours or theirs**.
Each gets its own recommendation below, then they recombine into one v1 shape.

---

## 1. Simulation time vs. API-call time

`SimulationEnv` (`sim/scheduler/env.py`) is a plain `heapq` priority queue keyed
on `(time_ns, priority, sequence)` with zero coupling to the wall clock — it only
advances when something pops an event. That's a blank slate: nothing forces
sim-time and real-time to relate at all.

| Option | Trade-off |
|---|---|
| A. Lockstep — one decision, one action | Sim pauses at the agent's turn, blocks on the LLM call, executes exactly the one tool call returned, resumes. Trivial to reason about, but every fraudulent action costs a full API round-trip. |
| B. Decoupled / live | Background traffic keeps advancing on its own clock while the agent "thinks" in wall time. `SimulationEnv.schedule()` already rejects scheduling into the past, so a slow response can't retroactively land where intended — needs a held-time window per actor, real added complexity for a single-threaded scheduler with no concurrency model today. |
| **C. Batched planning sessions (recommended)** | The agent isn't invoked per micro-action. Periodically, it's given the current world view and returns an ordered plan of tool calls with intended offsets, executed rapid-fire with no further LLM calls until the next planning point. |

**Gateway contract needed for C:** `ToolGateway.call_tool()` today takes one call
at a time. Batched planning needs a real entry point, not a harness-side loop
over `call_tool()`:

```python
def call_plan(
    self,
    plan: list[ToolCall],  # {tool_name, parameters, intended_sim_time_offset_ns}
    context: ActorContext,
) -> list[ToolResult]:
    """Execute multiple tools in one logical turn, one rate-limit window."""
```

Without this, batching logic leaks into the harness instead of living in the
gateway where capability checks and rate limits already are.

**Open question:** how much simulated time does one planning session span before
the agent re-plans? Too short reintroduces option A's cost problem; too long
lets background traffic drift far past what the last plan accounted for. Related:
nothing today pauses `PopulationManager.start_agent_loops()`'s recurring
schedule while the red agent "thinks" — background population events keep
advancing regardless. Batching doesn't freeze the world, it just changes how
often the agent is consulted.

---

## 2. When does the red team start?

| Option | Trade-off |
|---|---|
| A. From genesis, alongside the population | Spawned by `PopulationManager.create_population()` like any other entity from `t=0`. Most realistic for cold-start fraud (synthetic identity, attacker-as-account-owner from creation) — but burns LLM calls on an uninteresting bootstrap period and conflates organic ramp-up with injected fraud, muddying `diff-branches` attribution. |
| **B. Fork after a warm-up checkpoint (recommended)** | Run the deterministic population sim first, `create_checkpoint`, `fork` a branch, only spend LLM calls from that point forward. Matches the CLI idiom already documented (`fork-branch --checkpoint <id> --branch red-team`). Makes `diff_branches(main, red-team, event)` a clean before/after, and the same checkpoint can seed many independent red-team branches (different personas/strategies) without re-running bootstrap each time. |

**Correction from review:** don't default the warmup to 24h. Per
`CLAUDE.md`'s known gotcha, event scheduling has been observed to grow
superlinearly with duration at fixed population size (200 users: 6h → ~6k steps,
24h → ~800k steps — a 4× duration increase produced ~130× more events; root
cause suspected in `sim/population/temporal.py`, not yet fixed). Picking 24h as
the "cheap baseline" contradicts that. **Default warmup to 2–4h for v1**, make
duration configurable, and don't raise it until the superlinear-growth bug is
understood.

Keep option A available as an explicit scenario flag
(`run-red-team --from-genesis`) for the fraud patterns that specifically need a
cold start.

---

## 3. Forking as the agent's own instrument

The idea worth building: branching doesn't have to be only *our* tool for
observing the agent after the fact — it can be *the agent's* tool, a scratch
multiverse it uses to test a move, see if it gets flagged or frozen, and try a
different approach without committing the failed attempt to a timeline anyone
scores against.

```
checkpoint C0 (warm baseline, main)
   │
   ├─ fork → red-team/session-1/attempt-a   agent tries: rapid cash-out
   │                                        → flagged, rolled back
   ├─ fork → red-team/session-1/attempt-b   agent tries: structured transfers under threshold
   │                                        → clears undetected
   └─ commit → red-team/session-1           agent's chosen strategy, scored against main
```

This is more feasible than it looks: `Capability.FORK_BRANCH`, `REPLAY_BRANCH`,
and `DIFF_BRANCHES` already exist in the enum (`sim/gateway/interfaces.py`) —
`ROLE_CAPABILITIES[ActorRole.RED_AGENT]` just doesn't grant them yet. Turning
this on for the capability side is a one-line change. What actually blocks
shipping it:

| Gap | Detail | Fix |
|---|---|---|
| **`build_simulation_for_branch()` doesn't exist** | `sim/main.py::build_simulation()` always builds against `branch_id="main"` with a fresh `PostgresChronoDAG`. Running on a forked branch needs `chrono.checkout(branch_id)` → reconstruct a live `WorldEngineImpl` with restored aggregates + RNG state from the `ReplayContext`. Nothing does this today. | New helper in `sim/main.py`; real work, not a one-liner. |
| **Scheduler queue isn't part of the checkpoint replay path** | The `SimulationEnv._queue` (pending future events) isn't restored by `checkout()`/`replay()` — those rebuild aggregate + RNG state but not in-flight scheduled events. A resumed branch needs its pending event queue reconstructed too, or agent actions on that branch have no organic traffic still "in motion" around them. | Needs its own design pass — flagged here, not solved. |
| **Branch attribution** | A tree of exploratory forks makes "what did the red team actually do" ambiguous unless the committed strategy is distinguishable from throwaway attempts. `Branch.metadata` (`sim/chrono/interfaces.py`) already exists as a plain `dict[str, object]` — no schema change needed, just a convention: `metadata={"origin": "agent_experiment"}` vs. `{"origin": "committed"}`, set via a new `commit_strategy` tool call. | New `ToolSpec` in the registry; tags the branch, doesn't need a new capability (reuse `FORK_BRANCH`). |
| **Branch naming & cleanup** | Unbounded fork spam per session. | Convention `red-team/<session>/<attempt-n>`; abandoned attempts get `delete_branch`'d (already in the `ChronoDAG` protocol, `sim/chrono/interfaces.py:153`) at session close. |
| **Checkpoint cadence** | A checkpoint before every single experiment is heavy if the agent explores fine-grained micro-actions. | Checkpoint once per planning session (§1C), not per attempted action — bounds cost to the batching granularity already chosen. |
| **Rate limiting on branch ops** | `ToolGatewayImpl`'s rate limiter (`sim/gateway/gateway.py`) is simple per-step/per-day counters — a fork or checkpoint call costs the same budget as a payment call today, which is wrong; branch ops are far more expensive. | Add a rate-limit tier (e.g. `rate_limit_tier: "normal" \| "branch_op"` on `ToolSpec`) with its own, tighter limit, enforced alongside the existing per-step/per-day counters. |

**Don't conflate the two uses of forking.** Analyst-driven forking (§2, for our
observability) and agent-driven forking (§3, for its own exploration) call the
same `ChronoDAG` methods but serve different consumers — keep them
distinguishable via the `metadata.origin` convention from day one, or a later
`diff-branches` pass can't tell "the state where the attacker succeeded" from "a
dead end it abandoned."

---

## 4. Who owns the "session" concept?

There's no `Session` entity in the codebase. `ActorContext.session_id`
(`sim/gateway/interfaces.py:129`) exists but is unused today. Three places it
could live:

1. **Gateway-side** — track planning sessions via `ActorContext.session_id`.
2. **ChronoDAG-side** — branch metadata carries `session_id` / `attempt_number`.
3. **Harness-side** — an external script owns session lifecycle; branches are
   just branches, no session concept in the data model at all.

**Recommendation: (2).** Branch metadata is the source of truth; the gateway
stays stateless and doesn't need to track session state itself. This is
consistent with the `origin` tagging convention in §3 — both live in
`Branch.metadata`.

---

## 5. How the pieces recombine (v1 shape)

| Concern | Decision |
|---|---|
| Onset | Fork from a warm-up checkpoint (§2B, 2–4h default). Genesis-start stays available as an explicit flag. |
| Cadence | Batched planning sessions (§1C) via a new `call_plan()` gateway method. |
| Exploration | Within a session, the agent may fork sub-branches to test moves before committing (§3), gated by new capability grants + mandatory `commit_strategy` call. |
| Session identity | Lives in `Branch.metadata` (§4), not a new entity. |
| Isolation | All of this happens off `main`. Nothing the agent does — including its own forks — ever mutates the branch other actors or analysts read from. |

---

## 6. Build sequence

Ordered so nothing is built on a helper that doesn't exist yet:

| # | What | Files |
|---|---|---|
| 1 | Wire a `RED_AGENT` `ActorContext` + persistent actor identity, and confirm `get_world_view()` works for it | new harness code, no `sim/` changes expected |
| 2 | `build_simulation_for_branch(branch_id)` helper — checkout + reconstruct `WorldEngineImpl` (aggregates + RNG); design how the pending scheduler queue is handled | `sim/main.py` |
| 3 | Rate-limit tier for branch ops (`FORK_BRANCH`/`REPLAY_BRANCH`/`DIFF_BRANCHES` vs. normal tools) | `sim/gateway/interfaces.py`, `sim/gateway/gateway.py` |
| 4 | Grant `FORK_BRANCH`, `REPLAY_BRANCH`, `DIFF_BRANCHES` to `RED_AGENT` in `ROLE_CAPABILITIES` | `sim/gateway/interfaces.py` |
| 5 | `commit_strategy` tool — tags `Branch.metadata.origin` | `sim/gateway/registry.py` (+ handler in `sim/main.py`'s tool registration) |
| 6 | `call_plan()` on `ToolGateway` for batched execution | `sim/gateway/interfaces.py`, `sim/gateway/gateway.py` |
| 7 | Minimal red-agent loop: checkout → get_world_view → LLM → call_plan → repeat | new `scripts/red_team_run.py` |
| 8 | `LangGraphAdapter.as_tool_node()` (or a simpler harness first, defer LangGraph until the loop above is proven) | `sim/gateway/adapters.py` |

Steps 2 and the scheduler-queue sub-problem inside it are the real work here —
everything downstream depends on being able to actually run on a forked branch.
The capability grant (step 4) is genuinely a one-liner; don't let its simplicity
imply the rest of the sequence is too.

---

## Open questions not resolved here

- **Session length** — how much sim-time does one planning session span before
  re-planning? Affects both §1's cost model and how stale the agent's view of
  background traffic gets.
- **Scoring the committed branch** — diffed only against `main`, or also against
  sibling red-team branches from other personas/models, to compare which
  adversary found the sharpest edge?
- **Multi-provider personas** — running different models as distinct personas is
  just distinct `ActorContext`s against the same gateway, no architectural
  blocker — but does persona identity live in the branch name or in
  `Branch.metadata`? (Given §4's decision, metadata is the consistent answer.)
