# Red-Team Agents — Design Notes

Status: in progress. Scope: red-team only (blue-team deferred). Phase 0 of the
build sequence (§6) is implemented — package skeleton, provider config,
import-linter boundary. Everything else in §6 is still proposal. Builds on
`sim/gateway`, `sim/chrono`. Companion to `docs/chrono_dag.md` and
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

Ordered so nothing is built on a helper that doesn't exist yet. Checkboxes
track actual implementation status, not just planning status.

| # | Phase | What | Files | Status |
|---|---|---|---|---|
| 0 | Scaffolding | New `agents/redteam/` package, `redteam` optional-dependency extra (`litellm`, `langgraph`), import-linter contract fencing `sim` off from importing `agents`, `providers.yaml` + `RedTeamConfig` | `pyproject.toml`, `Makefile`, `agents/redteam/{__init__.py,config.py,providers.yaml}` | ✅ done |
| 1 | LLM router | `litellm.Router` wrapper over the provider/account pool (§7 below); `plan_next_moves()` with block-and-retry backoff on pool exhaustion | `agents/redteam/llm_router.py` | not started |
| 2 | Gateway contract | `ToolSpec.rate_limit_tier`, `RED_AGENT` capability grant for `FORK_BRANCH`/`REPLAY_BRANCH`/`DIFF_BRANCHES`, `ToolCall` dataclass + `call_plan()` on `ToolGateway`, `ChronoDAG.update_branch_metadata()` (new — no mutator exists today), `commit_strategy` tool | `sim/gateway/interfaces.py`, `sim/gateway/gateway.py`, `sim/gateway/policy.py`, `sim/chrono/interfaces.py`, `sim/chrono/store.py`, `sim/main.py` | not started |
| 3 | Branch reconstruction | `build_simulation_for_branch(branch_id)` — checkout + reconstruct `WorldEngineImpl`; new `DeterministicRNG.from_state()` and `WorldEngineImpl.restore_aggregate_snapshot()` (neither exists today); scheduler-queue gap scoped out of v1, documented (§ Known limitations) | `sim/main.py`, `sim/scheduler/rng.py`, `sim/core/engine.py` | not started |
| 4 | Identity | `bootstrap_red_agent_context()` — first production code anywhere constructing a `RED_AGENT` `ActorContext`; persisted locally, not in `sim`/the DB | `agents/redteam/identity.py` | not started |
| 5 | Harness loop | `run_session()`: fork or genesis-start → checkout → get_world_view → LLM plan → `call_plan()` → repeat until `commit_strategy` or session cap; CLI entrypoint | `agents/redteam/harness.py`, `scripts/red_team_run.py` | not started |
| 6 | LangGraph | `LangGraphAdapter.as_tool_node()` + `build_graph()` wrapping the proven Phase 5 loop in a `StateGraph` (observe → plan → act → loop/END) | `sim/gateway/adapters.py`, `agents/redteam/harness.py` | not started |
| 7 | Verification | Unit tests for `call_plan`/tier limits/branch round-trip/router retry; manual smoke test against one real provider; `testpaths`/`make typecheck` scope | `sim/gateway/tests/`, `agents/redteam/tests/`, `scripts/red_team_smoke_test.py` | not started |

Phase 3 (branch reconstruction) is the real work — everything downstream
depends on actually being able to run a live simulation on a forked branch.
Phase 2's capability grant is genuinely a one-liner; the rest of that phase
(new `ChronoDAG` mutator, tier-based rate limiting) isn't. LangGraph (Phase 6)
is sequenced *after* the bare loop is proven, per user confirmation that
LangGraph is wanted for v1 — not deferred indefinitely, just not built before
its foundation (`call_plan`, branch reconstruction, identity) is validated.

---

## 7. LLM provider backend

**Confirmed requirement**: LangGraph for orchestration, backed by free tiers
across **Groq, NVIDIA Build, Gemini, and OpenRouter**, routed through
**LiteLLM's `Router`** rather than hand-rolled per-provider clients — LiteLLM
confirmed to support Gemini as a first-class provider, and its `Router`
supports multiple deployments per `model_name` with different `api_key`s and
several routing/failover strategies (simple-shuffle, rate-limit-aware-v2,
least-busy, latency-based).

**Shape confirmed with the user**: a single red-team persona whose LLM calls
round-robin/failover across the *whole* provider+account pool purely for
quota survival — not one persona per provider. On full-pool exhaustion, the
harness blocks and retries with backoff rather than skipping the session or
failing the run.

**Rate limits, verified against each provider's own docs (not guessed)**:

| Provider | Scope | Free-tier limit | Source |
|---|---|---|---|
| Groq | **org-level, not per-key** — a second key on the same org adds no quota | 30 RPM, ~14,400 RPD (model-dependent) | console.groq.com/docs/rate-limits |
| OpenRouter | **account-level, not per-key** — explicitly stated in their own docs | 20 RPM; 50 RPD unfunded, 1,000 RPD after a one-time $10 purchase | openrouter.ai/docs/api-reference/limits |
| Gemini | **per-project**, not a stable published constant | must be read live per project | aistudio.google.com/rate-limit |
| NVIDIA Build | unofficial, no published number | ~40 RPM, developer-forum consensus only | forums.developer.nvidia.com |

**Consequence for the multi-account idea**: multiple *API keys on one
account* do not multiply quota for Groq or OpenRouter — their docs say so
directly. Multiple *separate accounts* (separate orgs/projects/emails) do,
because the pooling in `providers.yaml` is per-`api_key_env` deployment
entry, each resolvable to a different account's key. This is what's actually
implemented (`agents/redteam/providers.yaml`), with a standing caution
comment in that file: using multiple accounts to multiply a single free
tier's quota is a ToS gray area for at least Groq and OpenRouter — the user's
own sandbox project, their call, flagged rather than silently done.

**Placement**: `litellm.Router` runs in-process inside the harness
(`agents/redteam/llm_router.py`), not as a standalone proxy service —
consistent with the project's existing "no local docker-compose stack"
constraint (see `CLAUDE.md`).

---

## Known limitations

- **Forked red-team branches resume with an empty scheduler queue.**
  `SimulationEnv._queue` (in-flight scheduled-but-unfired events) is not part
  of `Checkpoint`/`ReplayContext` and `build_simulation_for_branch()` (§6
  Phase 3) does not reconstruct it. Properly fixing this means either
  serializing the queue into the checkpoint format or deterministically
  replaying `PopulationManager`'s recurring schedule forward (risking the
  superlinear event-growth bug on longer runs) — both are separate design
  passes, out of scope for v1. Practical effect: a red-team branch has no
  organic background traffic "in motion" around the agent's actions unless
  the harness explicitly calls `PopulationManager.start_agent_loops(engine)`
  again after rebuilding (an approximation, not a restoration). A committed
  strategy that "clears undetected" on such a branch is weaker evidence than
  one tested against branches with organic noise present — interpret results
  accordingly until this is solved.

---

## Open questions not resolved here

- **Session length** — how much sim-time does one planning session span before
  re-planning? Affects both §1's cost model and how stale the agent's view of
  background traffic gets. `RedTeamConfig.session_max_plan_calls` (default 8)
  is a first guess, to be tuned from real smoke-test behavior, not validated.
- **Scoring the committed branch** — diffed only against `main`, or also against
  sibling red-team branches from other personas/models, to compare which
  adversary found the sharpest edge?
- **Multi-provider personas** — resolved for v1: one persona pooling across
  all providers (§7), not distinct personas per provider. Running distinct
  personas remains future work — would be distinct `ActorContext`s against
  the same gateway, no architectural blocker, with persona identity living in
  `Branch.metadata` per §4's decision.
