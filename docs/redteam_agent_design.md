# Red-Team Agents — Design Notes

Status: **v1 complete, plus a hardening pass driven by real sessions** — all
8 phases of the build sequence (§6) implemented and verified, then run for
real against real providers (Groq/Gemini/NVIDIA) and a real Postgres/Supabase
database repeatedly, which surfaced and fixed several real bugs (§9) and one
real architectural gap the agent itself found and demonstrated (§10). Builds
on `sim/gateway`, `sim/chrono`. Companion to `docs/chrono_dag.md` and
`docs/interfaces.md` — read those first for the mechanics this uses.

**§1–§4 decisions are locked** (interaction model = lockstep, onset = fork
after warmup, forking = the agent's own instrument, session identity = branch
metadata). See each section for what "locked" means concretely.

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

## 1. Simulation time vs. API-call time — LOCKED: lockstep (Option A)

`SimulationEnv` (`sim/scheduler/env.py`) is a plain `heapq` priority queue keyed
on `(time_ns, priority, sequence)` with zero coupling to the wall clock — it only
advances when something pops an event. That's a blank slate: nothing forces
sim-time and real-time to relate at all.

| Option | Trade-off |
|---|---|
| **A. Lockstep — one decision, one action (locked)** | Sim pauses at the agent's turn, blocks on the LLM call, executes exactly the one tool call returned, resumes. |
| B. Decoupled / live | Background traffic keeps advancing on its own clock while the agent "thinks" in wall time. `SimulationEnv.schedule()` already rejects scheduling into the past, so a slow response can't retroactively land where intended — needs a held-time window per actor, real added complexity for a single-threaded scheduler with no concurrency model today. Rejected: unneeded complexity for what the pool ceiling already forces regardless. |
| C. Batched planning sessions | The agent isn't invoked per micro-action — periodically given the world view, returns an ordered plan of tool calls, executed rapid-fire with no further LLM calls until the next planning point. **Rejected** — see below. |

**Why lockstep over batching, reversing the earlier recommendation:** batching's
appeal was "fewer API calls," but the free-tier pool (§7) is the actual
bottleneck regardless of interaction model — Groq 30 RPM, OpenRouter 20 RPM,
Gemini ~15 RPM, NVIDIA ~40 RPM cap the agent to roughly one action every few
seconds either way, so batching doesn't save real throughput. What it does cost
is reactivity: a pre-committed batch executes blind to whatever happened after
its first action — if move 1 gets flagged, the rest of the batch fires anyway,
unable to adapt. For an agent whose entire job is to probe and react to
detection, that's a correctness problem, not a minor trade-off. Lockstep's only
real downside (one LLM round-trip per action) is latency already baked into the
rate-limit ceiling, not an added cost.

**Consequence for the build:** no new `call_plan()` gateway method needed. The
existing `ToolGateway.call_tool()` — one call, one rate-limit window, one
capability check — is exactly the lockstep contract. This drops one whole piece
from Phase 2 of the build sequence (§6).

**Resolved, not actually open:** earlier drafts of this doc worried that
background population events would keep advancing in wall-clock-adjacent sim
time while the agent "thinks" between actions, drifting the world out from
under it. That doesn't apply to the actual architecture. Two facts settle
it: (1) `sim_time_ns` only moves when `SimulationEnv.step()` pops an event —
it has no coupling to the wall clock, so blocking on an LLM call doesn't
advance or drift anything; (2) a forked red-team branch gets a brand-new,
empty `SimulationEnv()` (`build_simulation_for_branch()`, §6 Phase 3/Known
limitations) — no restored queue, no `PopulationManager` loop attached. The
branch is static except for what the agent's own tool calls produce, so
there's no background traffic to drift out of sync *with* in the first
place. Blocking on the LLM call during "decide" costs nothing, because
nothing else on that branch is happening concurrently. This would become a
real question again only if forked branches were later given their own live
background traffic (see `PopulationManager.start_agent_loops(engine)` as a
mentioned-but-unbuilt mitigation in Known limitations) — not the case today.

---

## 2. When does the red team start? — LOCKED: fork after warm-up (Option B)

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

## 3. Forking as the agent's own instrument — LOCKED (this is the user's own idea)

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

## 4. Who owns the "session" concept? — LOCKED: branch metadata (Option 2)

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
| Cadence | Lockstep (§1A) — one `call_tool()` per agent decision, no new gateway method needed. |
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
| 1 | LLM router | `litellm.Router` wrapper over the provider/account pool (§7 below); `decide_next_action()` — one lockstep decision per call, with block-and-retry backoff on pool exhaustion | `agents/redteam/llm_router.py` | ✅ done |
| 2 | Gateway contract | `ToolSpec.rate_limit_tier`, `RED_AGENT` capability grant for `FORK_BRANCH`/`REPLAY_BRANCH`/`DIFF_BRANCHES`, `ChronoDAG.update_branch_metadata()` (new mutator, implemented on both `PostgresChronoDAG` and `InMemoryChronoDAG`), `commit_strategy` tool in a new `_register_redteam_tools()`. No new `call_plan()` method — lockstep uses the existing `call_tool()`. Tier-wide rate limiting (`RateLimiter.check_and_increment_tier`, `TIER_LIMITS`) enforced alongside per-tool limits. | `sim/gateway/interfaces.py`, `sim/gateway/gateway.py`, `sim/gateway/policy.py`, `sim/chrono/interfaces.py`, `sim/chrono/store.py`, `sim/chrono/tests/_fake_dag.py`, `sim/main.py` | ✅ done |
| 3 | Branch reconstruction | `build_simulation_for_branch(config, branch_id)` — checkout + reconstruct `WorldEngineImpl`, restoring RNG via the already-existing `DeterministicRNG.set_state()` (no new RNG method needed — `set_state()` was already there, just unused) and aggregates via new `WorldEngineImpl.get_full_snapshot_bytes()`/`restore_full_snapshot_bytes()`. Raises `NotImplementedError` if the checkpoint has pending events past it (no StoredEvent→DomainEvent replay path exists — checkpoint-at-head is the only supported case). Scheduler-queue gap scoped out of v1, documented (§ Known limitations) | `sim/main.py`, `sim/core/engine.py` | ✅ done |
| 4 | Identity | `bootstrap_red_agent_context()` — first production code anywhere constructing a `RED_AGENT` `ActorContext`; actor_id persisted locally (gitignored `agents/redteam/.persona_identity.json`), not in `sim`/the DB. Confirmed end-to-end: `get_world_view()` already correctly filters accounts to the actor's own for `RED_AGENT` (`sim/core/engine.py`'s owner-filter branch already included it) | `agents/redteam/identity.py` | ✅ done |
| 5 | Harness loop | `run_session()`: fork or genesis-start → checkout → get_world_view → LLM decides one action → `call_tool()` → observe result → repeat until `commit_strategy` or step cap; CLI entrypoint. `router`/`identity_file` injectable for tests — 3 tests cover stop-on-commit, stop-at-cap, and the checkpoint-or-genesis guard, all with a mocked LLM response (no real network calls) | `agents/redteam/harness.py`, `agents/redteam/personas.py`, `scripts/red_team_run.py` | ✅ done |
| 6 | LangGraph | `LangGraphAdapter.as_tool_node()` (generic — takes an `ActorContext`, reads `state["action"]` as a plain `{tool_name, parameters}` mapping so `sim/gateway` never imports `agents/`) + `build_graph()`/`run_session_via_graph()` wrapping the proven Phase 5 loop in a `StateGraph` (observe → decide → act → loop/END). Same 2 test scenarios as Phase 5, run through the graph instead of the bare loop, same results | `sim/gateway/adapters.py`, `agents/redteam/harness.py` | ✅ done |
| 7 | Verification | Unit tests for tier limits/branch round-trip/router retry (spread across earlier phases as they landed — 88 tests total across `sim/gateway/tests/`, `sim/chrono/tests/`, `agents/redteam/tests/`, `tests/integration/`); manual smoke test — ran for real against Groq/Gemini/NVIDIA + a real Postgres/Supabase DB, full session succeeded (`create_account` → `inspect_account`, branch `red-team/smoketest` created); `testpaths`/`make typecheck` scope (done in Phase 0) | `sim/gateway/tests/`, `agents/redteam/tests/`, `scripts/red_team_smoke_test.py` | ✅ done |

Phase 3 (branch reconstruction) is the real work — everything downstream
depends on actually being able to run a live simulation on a forked branch.
Phase 2's capability grant is genuinely a one-liner; the rest of that phase
(new `ChronoDAG` mutator, tier-based rate limiting) isn't. LangGraph (Phase 6)
is sequenced *after* the bare loop is proven, per user confirmation that
LangGraph is wanted for v1 — not deferred indefinitely, just not built before
its foundation (branch reconstruction, identity, the lockstep loop) is validated.

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

**Model IDs need periodic re-verification.** All four providers' free-tier
catalogs churn — every model ID originally guessed for `providers.yaml` had
already been deprecated/retired by the time real keys were tested against
them (Groq's `llama-3.3-70b-versatile` no longer exists; Gemini's
`gemini-1.5-flash` 404s with a message naming its replacement; NVIDIA's
`meta/llama-3.1-70b-instruct` had reached end-of-life; NVIDIA also has a
gap between what `GET /v1/models` *lists* and what a given API key is
actually *entitled* to call — a model can 404 as "not found for account"
despite appearing in the catalog). `providers.yaml`'s header comment has the
exact `curl` command per provider to re-check when a deployment starts
404ing.

---

## 8. Observability — where the agent's "thinking" is visible

**Primary mechanism (per explicit instruction): a React panel in the
existing FinSim frontend, not Grafana.** No prompt/reasoning content is
printed to the terminal either way — that constraint held regardless of
backend. The frontend live-streams each step's `tool_name`, `reasoning`,
success/failure, and which pool deployment served the call (routing), with
latency, over a WebSocket — verified against a real session (real Postgres,
real Groq/NVIDIA calls) end to end via `curl` before wiring the UI.

**Backend — new session runner + pub/sub, separate composition root from the
demo sim:**
- `agents/redteam/llm_router.py`: `NextAction` gained `provider_model` (which
  deployment actually served the call, e.g. `nvidia_nim/meta/llama-3.2-11b-vision-instruct`)
  and `latency_ms`, captured in `decide_next_action()` from the litellm
  response and a wall-clock timer around the call.
- `agents/redteam/harness.py`: `run_session()`/`run_session_via_graph()` both
  gained an `on_step` callback, invoked synchronously after each step with
  the same dict appended to `SessionResult.step_log` — the hook the API
  broadcasts through. Optional, so the harness has no observability
  dependency by default (nothing changes for `scripts/red_team_run.py`).
- `api/redteam_session.py` (new): `RedTeamObserver` — in-process session
  registry + pub/sub, mirroring `api/live_dag.py::LiveChronoDAG`'s
  subscriber pattern. Runs sessions in a thread-pool executor (litellm calls
  are blocking) and reports back to the event loop via
  `loop.call_soon_threadsafe`, the same pattern `SimSession` uses for its
  own blocking DAG operations. **Deliberately separate from `SimSession`**:
  the demo sim runs on an in-memory `LiveChronoDAG`, while red-team sessions
  always talk to the real Postgres/Supabase `FINSIM_DB_URL` (same backend
  `agents/redteam/harness.py` always used) — unifying those two ChronoDAG
  backends is out of scope here.
- `api/main.py`: `POST /api/redteam/sessions` (start; `from_genesis` or
  `checkpoint_id`, `seed`, `use_graph`), `GET /api/redteam/sessions` (list),
  `GET /api/redteam/sessions/{id}` (detail + full step log), `WS
  /api/redteam/stream/{id}` (live steps; replays already-happened steps to a
  subscriber that connects mid-session, since there's no periodic "tick" to
  eventually catch up on the way `/api/stream` has). Also
  `POST /api/checkpoints/{id}/export-for-redteam` (bridges a demo-session
  checkpoint — which lives only in the in-process `LiveChronoDAG` — into the
  real Postgres store the harness can actually fork from; see §9) and the
  `/api/branches` graph endpoint now also merges in real `red-team/*`
  branches from Postgres alongside the demo ones, so a red-team fork
  actually shows up as its own lane in the ChronoDAG graph instead of only
  existing in the DB (clicking it jumps to the Red Team view below and
  selects that session).

**Frontend — its own top-level view, not a side-panel tab.** Originally
shipped as `RedTeamPanel.tsx`, one more tab alongside Feed/Agents/
Checkpoints/Sandbox — too cramped for what turned out to matter once
sessions were run for real. Replaced with `frontend/src/RedTeamDashboard.tsx`,
switched to via a "Simulation / 🔴 Red Team" toggle in the top bar
(`App.tsx`'s `view` state) that swaps the whole layout, not just the side
panel. The dashboard shows, per session: a header with copyable session/
branch IDs and commit status, a step-progress meter (`steps_taken /
max_steps` — `max_steps` is now threaded through from
`RedTeamConfig.session_max_steps` into `RedTeamSessionState`, previously
absent), stat tiles (success rate, ok/failed count, providers used), two
charts (tool-usage counts, provider-routing avg latency — hand-rolled bar
components consistent with the app's existing hand-rolled `DagGraph`/
`Sparkline`, no charting library dependency), and the live step feed. Each
step now shows the actual `error_message` (not just `error_code` — it
simply wasn't being sent to the frontend before, see §9), and a failure
with `error_code == "INTERNAL_ERROR"` renders a visually distinct 🐛
badge instead of the same "closed/fail" styling a normal business
rejection gets — the whole point being a human glancing at the feed can
tell "this is a bug" from "this is the agent's transaction being declined,
working as intended" without reading the message text. Still extends the
app's existing visual system (near-black palette, JetBrains Mono/Inter,
the same `.feed-item`/`.badge`/`.pill`/`.stat-tile` component classes) —
no second visual language, just more surface area.

**litellm/OTel tracing (`_maybe_register_otel_callback()` in
`llm_router.py`) is kept, demoted to secondary/optional**, still gated on
`RedTeamConfig.enable_otel_tracing` (default `True`) — useful if you also
want this in Grafana Cloud's OTLP pipeline (e.g. cross-referencing against
the main sim's traces, longer retention than the in-memory session
registry), but the frontend is what to reach for by default. Not gated on
`OTEL_EXPORTER_OTLP_ENDPOINT`'s mere presence — a real gotcha found while
building this: importing `litellm` loads `.env` as a side effect
(undocumented litellm behavior), which sets that env var in *every* process
the moment `litellm` is imported, regardless of test intent. All three
router/harness test fixture files explicitly pass `enable_otel_tracing=False`.

---

## 9. Real bugs found by actually running sessions

v1 "worked" (start a session, get a step feed) but the *first* real
multi-provider sessions against a real DB surfaced a run of genuine bugs —
listed here because each one changed what a session's output actually meant,
not just how it looked. Ordered roughly as found.

- **`create_account` was a pure no-op stub.** `sim/main.py::_register_core_tools`'s
  handler was `def create_account_handler(context, params, engine): return []`
  — never called `engine.create_account()`. Every call reported success while
  creating nothing, so the agent's own account list stayed permanently empty
  and it just looped `create_account → inspect_account` forever, never
  reaching `transfer_funds`/`make_payment`. This was the actual reason early
  sessions "did nothing interesting" — not the step cap (raised 8 → 30
  alongside the fix, but that was never the real bottleneck).
- **`inspect_account`/`transfer_funds`/`make_payment` silently reported
  failure as success.** `inspect_account` returned `[]` (empty-but-successful)
  when the target account didn't exist instead of erroring.
  `transfer_funds`/`make_payment` returned `CommandResult.events`
  unconditionally, ignoring `CommandResult.success` — so a *rejected*
  transfer (insufficient funds, over a daily limit) was reported to the
  agent as a success containing a rejection event, the one signal a
  red-team agent most needs to read correctly. Fixed to check `.success`
  and raise on failure.
- **Every failure surfaced as an uninformative `INTERNAL_ERROR`.** Two
  separate causes: (1) the fix above raised plain `ValueError`s, which the
  gateway's generic exception handler collapses into `INTERNAL_ERROR`
  regardless of whether it's a business rejection or a real bug; (2)
  unguarded parameter parsing — `int(str(params.get("amount_paise")))` with
  a missing `amount_paise` silently became `int("None")`, a raw crash with
  no real signal. Fixed with a new `ToolRejection` exception
  (`sim/gateway/interfaces.py`) carrying a real `error_code`
  (`LIMIT_EXCEEDED`, `INSUFFICIENT_FUNDS`, `ACCOUNT_NOT_FOUND`,
  `MISSING_PARAMETER`, `INVALID_PARAMETER`, ...) pulled from the domain
  event's own `reason_code`/`decline_code`/`detail` fields
  (`sim/core/events.py` already had this data, it just wasn't surfaced),
  caught specifically by `ToolGatewayImpl.call_tool()` before the generic
  handler. Genuine unexpected exceptions now go through the new
  `sim/gateway/errors.py::internal_error_result()` — logs a full traceback
  server-side via the project's structured logger (which itself needed a
  fix: no `format_exc_info` processor in the structlog config, so
  `exc_info=True` either lost the traceback or risked breaking JSON
  serialization) and writes an `error_message` explicit that this is a
  system fault, not a rejection — read by both a human (the 🐛 badge, §8)
  and the LLM (persona prompt now says explicitly: `INTERNAL_ERROR` isn't a
  business signal, don't reason about it as a detection threshold, try
  something else).
- **`error_message` was never sent to the frontend at all** — `step_entry`
  in `harness.py` only ever included `error_code`. Combined with the point
  above, this meant there was no way to tell from the UI whether a failure
  was an expected rejection or a real bug — the data simply wasn't there.
  Fixed (§8).
- **The agent had no memory beyond one step back.** `decide_next_action()`
  only ever received `last_action_outcome` (singular). A rotating pool of 4
  different providers sharing no state beyond one line meant the agent
  would routinely repeat an action it had already tried and failed at
  several steps ago. Fixed with a bounded rolling `history` (last 12 steps,
  `agents/redteam/harness.py::_HISTORY_WINDOW`, now naming the actual
  account(s) involved per line, not just tool name + success) plus a new
  `save_note` tool (`sim/main.py`) — the agent writes a note naming an
  account_id and its plan for it, persisted to the branch's own ChronoDAG
  metadata (same mechanism `commit_strategy` uses — inspectable after the
  session ends, not just LLM-context-bound) and echoed into every
  subsequent turn's prompt via `personas.summarize_target_notes()`.
- **`_get_daily_reset_events()` crashed the live population loop on every
  day-boundary rollover.** `DailyCountersReset` (like every `DomainEvent`
  subclass) is `@dataclass(frozen=True)`, but the code tried
  `reset_event.event_id = self._next_event_id()` — direct mutation of a
  frozen instance, raising `FrozenInstanceError`. Not red-team-specific
  (affects the demo `main` branch's background loop too, `api/sim_session.py`),
  found via a real crash log. Fixed with `dataclasses.replace()`; regression
  test in `sim/core/tests/test_engine.py` reproduces it by advancing
  `sim_time_ns` past a day boundary.
- **Checkpoint export corrupted the idempotency cache's type.**
  `WorldEngineImpl._processed_idempotency_keys` is a `dict[str, CommandResult]`
  everywhere in `engine.py` (its own `__init__` annotation was even wrong —
  `dict[str, None]` — fixed alongside this, and had been silently flagged by
  mypy the whole time), but `api/sim_session.py`'s own snapshot/restore code
  (`_snapshot_engine_state`, `build_engine_from_checkpoint`, `fork()`) stored
  and restored it as a `set` in three places. Latent until the "export for
  red team" bridge (§8) chained it into a real crash: demo checkpoint →
  `build_engine_from_checkpoint` (now a `set`) → `get_full_snapshot_bytes()`
  (pickles the `set` as-is) → `restore_full_snapshot_bytes()` on the real
  red-team engine → the next `transfer_funds`/`make_payment` call raised
  `TypeError: 'set' object does not support item assignment` trying to
  cache its result. Fixed (`dict(...)` in all three spots); regression test
  in `api/tests/test_sim_session.py` reproduces the full chain and was
  confirmed to fail pre-fix.

## 10. RED_AGENT's account visibility, and the authorization gap it found

Originally `RED_AGENT` saw only its own accounts, same as `USER`/`MERCHANT`
(`WorldEngine.get_world_view()`). On explicit request to widen scope — "not
just merchants, users too, think about what real control-of-payments looks
like" — this was deliberately expanded: `RED_AGENT` is now **white-box**,
seeing every account on the branch (other users' *and* merchants'), with
only the owner's real identity/device IDs masked the same way `BANK_OPS`
masks them (real `account_id`/`balance`/`status`/`kyc_level` throughout —
the agent needs those to pick and reason about a target). Framing choice,
recorded because it's not obviously "realistic": this models a white-box
security engagement (elevated knowledge, like a real pentest), not a blind
external attacker's actual discovery process — appropriate for stress-testing
detection *coverage*, not for measuring what a real fraudster could
plausibly find on their own. `summarize_world_view()` (`agents/redteam/personas.py`)
caps the non-owned list at 20 entries so a large population doesn't blow up
the prompt every turn; own accounts are never capped.

**This surfaced a real, separate authorization gap**: `_execute_transfer()`/
`_execute_payment()` never checked that `command.actor_id` owned
`command.source_account_id` — before the visibility widening this was moot
(no actor with transfer capability ever learned another account's id), but
once RED_AGENT could see other accounts, nothing stopped it from naming one
as the *source* and draining it, not just paying into it. A real session
found and demonstrated exactly this within ~5 steps, then spent the rest of
a 15-step session repeating the identical move rather than exploring
anything else — which was itself informative: a missing authorization check
makes every other, more interesting fraud pattern (structuring, cash-out
timing, KYC-limit evasion) pointless to try, because the trivial win
dominates. **Decision: closed the gap**, not left open. `_execute_transfer()`/
`_execute_payment()` now require `command.actor_id == source_account.owner_id`
(`reason_code`/`decline_code == "UNAUTHORIZED_SOURCE"` otherwise) — matching
how a real payments system actually works. Visibility is unaffected: any
account is still a valid transfer/payment *target* (paying another user or a
merchant is always allowed), only the *source* is restricted to accounts the
actor owns. Verified this doesn't affect normal population behavior —
`PopulationManager._agent_step` always builds commands where `actor_id`
already equals the source account's owner by construction. The persona
prompt (`agents/redteam/personas.py`) and tool hints were rewritten to match:
previously said finding this was "not an exploit to avoid"; now explicitly
tells the agent the door is closed and not to waste steps probing for a
bypass, redirecting toward the patterns that are actually the point —
structuring, cash-out speed, KYC/daily-limit edges, laundering through a
target account as a payment *destination*.

---

## 11. Session continuation — resuming and pooling findings across sessions

Two real gaps existed until now: (1) `commit_strategy`'s actual reasoning
text — the LLM's own summary of what it demonstrated, the thing worth
keeping — was never written anywhere durable, only branch metadata's
`origin: "committed"` flag was; (2) a red-team branch was never
checkpointed after its initial fork, so there was no checkpoint_id to hand
to a new session's `--checkpoint` even if you wanted to continue from where
one left off — "resume" was structurally impossible, not just unbuilt.

**Design choice, and why**: the alternative was one long-running session
(raise `session_max_steps` a lot, no resume concept needed at all — the
agent just keeps finding more within a single continuous run). Rejected
that in favor of bounded sessions + explicit continuation:
- A single very long session still hits the same "forgets beyond the
  history window" problem (§9) internally — going longer doesn't remove
  the need for durable memory, it just makes losing it more expensive.
- Bounded sessions give natural checkpoints for a human to actually look
  at what happened before deciding whether to continue, redirect, or stop
  — losing that in exchange for one uninterrupted run is a real cost, not
  a simplification.
- A checkpoint per session end also means multiple *next* sessions can
  fork from the same commit point to explore different directions in
  parallel, which a single continuous run can't do at all.

**What's now built**: every session (`run_session()`/`run_session_via_graph()`
in `agents/redteam/harness.py`, both paths) ends with `_checkpoint_branch_end()`
— a real checkpoint of the branch's state, whether it ended via
`commit_strategy` or just hit the step cap — tagged with
`{"origin": "session_end", "session_id", "committed"}` and returned as
`SessionResult.end_checkpoint_id` (threaded through `RedTeamSessionState`/
the frontend). On a successful `commit_strategy`, `_record_commit_reasoning()`
additionally writes the LLM's own reasoning text onto the branch's metadata
as `commit_reasoning` — the exact text visible in the UI step feed, now also
durable.

**Pooling, not just state restoration**: forking from a checkpoint alone
only restores engine *state* (account balances exactly as they were) — a
naively "continued" session would have zero idea what a prior pass already
found and would waste steps rediscovering it. `_seed_notes_from_parent()`
closes that: if the checkpoint being forked from lives on another
`red-team/*` branch (not a `main` warmup), the new session's `notes` list
is seeded with that branch's `target_notes` and `commit_reasoning` before
the first turn even runs — the agent's very first world-view prompt already
says what a previous pass on this lineage concluded. In the UI, a finished
session with an `end_checkpoint_id` shows a "▶ Continue from here" button
(`RedTeamDashboard.tsx`) that pre-fills the start form with it.

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

- **Two incompatible checkpoint serializations exist for `aggregate_snapshot`.**
  `WorldEngineImpl.get_canonical_state_bytes()` (used by `api/sim_session.py`
  for its own live in-process forking) is a lossy, hash-only encoding —
  `Account.to_canonical_dict()` and its siblings deliberately drop
  `account_id`, `owner_id`, `created_at_ns`, `linked_device_ids`, etc.,
  fields not needed for the state-hash contract but required to reconstruct
  a usable `Account` object. `build_simulation_for_branch()` (§6 Phase 3)
  needs the *other* one — the new `get_full_snapshot_bytes()`/
  `restore_full_snapshot_bytes()` pair, which pickles the aggregates
  wholesale. **Checkpoints created via the API's live-session forking path
  are not restorable through `build_simulation_for_branch()`** — a red-team
  session's warmup checkpoint must be created with `get_full_snapshot_bytes()`
  explicitly (see the round-trip test in
  `tests/integration/test_build_simulation_for_branch.py` for the exact
  shape). This wasn't originally called out in this doc's Phase 3 gap
  description — it was assumed decoding whatever `aggregate_snapshot` already
  contained would be enough; it isn't, because two different producers of
  that field disagree on its format. The UI's "Use for Red Team" flow
  (§8/§9) handles this correctly by construction: `api/main.py`'s
  `/api/checkpoints/{id}/export-for-redteam` always rebuilds the demo
  checkpoint's engine and calls `get_full_snapshot_bytes()` on it before
  writing to Postgres — the wrong-serialization mismatch only bites if
  something bypasses that endpoint and hands a raw demo `checkpoint_id`
  straight to the harness.

- **`build_simulation_for_branch()` only supports checkpoint-at-head.**
  If the target branch has any events committed after its latest checkpoint
  (`ReplayContext.pending_events` non-empty), it raises `NotImplementedError`
  rather than silently reconstructing incomplete state — nothing in the
  codebase deserializes a `StoredEvent.payload` dict back into a live
  `DomainEvent` instance to replay into a rebuilt engine. In practice this
  means: always take a fresh checkpoint immediately before forking, and
  don't execute further commands against a branch's engine without
  re-checkpointing before the next `build_simulation_for_branch()` call
  against it (e.g. from a separate process).

- **`ToolSpec.parameter_schema` is registered as an empty `{}` for every
  tool in `sim/main.py` today** — real JSON Schema was never populated
  there, for any actor, not just red-team. The agent can't be told what
  parameters a tool expects by reading the schema off `list_tools()`.
  Worked around in `agents/redteam/personas.py` with a hardcoded
  `_TOOL_PARAM_HINTS` table for the known core + red-team tools, injected
  into the prompt alongside (not instead of) the real schema. This is a
  stopgap, not a fix — the actual fix is populating `parameter_schema` on
  every `ToolSpec` registration in `sim/main.py`, which is outside this
  phase's scope but should happen before new tools are added.

- **Grafana Cloud export isn't guaranteed to actually reach the collector.**
  Verified real, non-code-level failures during testing: a `401
  Unauthorized` (points at `OTEL_EXPORTER_OTLP_HEADERS` in `.env` being
  stale/invalid — verify the Grafana Cloud token is current) and, separately,
  an IPv6 "no route to host" (environment-specific network egress, likely
  not reproducible outside that sandbox). Neither is a bug in §8's wiring —
  `decide_next_action()` returns correct results regardless of whether the
  span actually made it to Grafana, since export failures don't raise. If
  spans aren't showing up, check credentials and network reachability
  before assuming the instrumentation is broken.

- **`--from-genesis` isn't repeatable against a persistent "main" branch —
  by design, not a bug.** `event_id` derivation
  (`WorldEngineImpl._next_event_id`) is `uuid5(branch_id, seq_num)`, with no
  dependency on `seed` or event content. Re-running genesis population
  creation against a real DB that already has "main" history always
  collides on `UniqueViolation` at the same `seq_num`s, regardless of seed.
  This isn't specific to the red-team harness — `sim/main.py::run_seed()`
  has the identical property. Correct usage: run `--from-genesis` once per
  environment to bootstrap, note the checkpoint id it logs, then use
  `--checkpoint <id>` for every subsequent session — never re-run genesis
  against a "main" that already has history. Found by actually running
  `scripts/red_team_smoke_test.py` against a real Supabase DB that already
  had 665 events on "main" from prior work; required a `chrono.reset()`
  (destructive, done with explicit user confirmation) to get a clean smoke
  run.

---

## Open questions not resolved here

- **Session length — resolved via real behavior, no longer open.**
  `RedTeamConfig.session_max_steps` was `8`, a smoke-test-only value nobody
  revisited. Real sessions showed why that was wrong for a different reason
  than expected: 8 steps never even got past `create_account`/
  `inspect_account` setup — but that turned out to be the no-op-handler bug
  (§9), not a genuinely-too-low cap. Raised to `30` alongside that fix,
  which is enough for a session to reach and explore actual fund-movement
  strategies within free-tier rate limits (lockstep = 1 call/step). Whether
  30 is *still* right now that `UNAUTHORIZED_SOURCE` (§10) closes the
  trivial win — i.e. whether sessions need more room to find the subtler
  patterns (structuring, cash-out timing) once the easy exploit is gone —
  is itself now the open question, not the original number.
- **Scoring the committed branch** — diffed only against `main`, or also against
  sibling red-team branches from other personas/models, to compare which
  adversary found the sharpest edge?
- **Multi-provider personas** — resolved for v1: one persona pooling across
  all providers (§7), not distinct personas per provider. Running distinct
  personas remains future work — would be distinct `ActorContext`s against
  the same gateway, no architectural blocker, with persona identity living in
  `Branch.metadata` per §4's decision.
