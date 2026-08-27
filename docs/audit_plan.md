# Audit Findings & Remediation Plan

**Status: All 5 phases complete** (see bottom of file for a summary of what was found and fixed, including several significant issues discovered mid-implementation that weren't in the original audit).

Generated from a full-codebase audit (8 parallel subsystem reviews) comparing `initial_plan.md`, `docs/*.md`, and actual implementation as of 2026-08-28. Decisions below were confirmed with the repo owner before drafting this plan.

## Phase 1 — Correctness-critical fixes (breaks determinism/replay today)

1. **Wire ChronoDAG into the live engine.**
   `sim/core/engine.py` currently has zero dependency on chrono (`execute_command()` never calls `dag.save_event()`). Give `WorldEngine` a `ChronoDAG` dependency and call `save_event()` for every event emitted during `execute_command()`, matching the plan's Command→Validate→Emit→Append→Apply pipeline. Update `sim/main.py` composition root to pass the store in. Add/adjust tests to assert persisted events match in-memory events.

2. **Fix population entity RNG keying.**
   Standardize on `spawn_for_entity("user"|"merchant", entity_id)` (lowercase, matching `agents.py:77,101`) everywhere. Fix `behaviour.py:137` (uppercase `ActorRole.value`) and `behaviour.py:296` (default `"actor"` fallback) to use the same scheme. Add a regression test asserting the same entity gets an identical RNG stream regardless of call path.

3. **Add `owner_id` to `AccountSnapshot` + implement role-based masking.**
   Add the field, then implement actual PII masking in `get_world_view()` based on actor role (currently only `linked_device_ids` gets a deterministic hash — not real masking). Extend `contract_test_core.py` to verify masking per role.

4. **Implement the 4 missing transaction handlers**: `_execute_chargeback`, `_execute_settlement`, `_execute_fee`, `_execute_interest` in `sim/core/engine.py`, following the pattern of the existing 6. Wire `AccountFreezeFailed` into `account.py`'s `apply_event()` (currently unhandled).

## Phase 2 — CLI consolidation (per your choice: move CLI into `sim/main.py`)

5. Build out `sim/main.py` as the real composition-root CLI per the original plan: `run-seed`, `fork-branch`, `replay-branch`, `diff-branches`. Port logic currently in `scripts/run_simulation.py` (which becomes a thin wrapper or is retired — confirm which when we get there). Remove the `NullChronoDAG` dead stub once the real store is always wired (Phase 1).

## Phase 3 — Observability (full instrumentation)

6. Apply `@traced` to the plan's named entrypoints: `WorldEngine.execute_command`, `BehaviourModel.propose_actions`, `ChronoDAG.fork` (and reasonable additional entrypoints: `checkout`, `create_checkpoint`, `call_tool`).
7. Wire metric increments at actual call sites: `events_processed`, `tool_calls`, `forks_created` counters; `scheduler_queue_size` gauge; `event_latency` histogram.
8. Call `start_metrics_server()` from the composition root (`sim/main.py`).
9. Fix the stale `tracing.py` comment claiming a localhost-Jaeger default — it now defaults to Grafana Cloud OTLP.

## Phase 4 — Test coverage (scale to match Definition-of-Done)

10. Rewrite `tests/integration/test_determinism.py` and `test_full_simulation.py` to run at claimed scale (10,000 users / 100,000 events) or a clearly-documented representative subset, split into a fast default suite + a slow/nightly suite if runtime becomes prohibitive.
11. Rewrite `tests/integration/test_branching.py` to exercise the real `ChronoDAG` fork/diff (currently mocked DB only) — fork at event 5,000, apply 1,000 tool calls, assert main branch unmutated, assert diff accuracy.
12. Deepen `contract_test_chrono.py` and add `contract_test_gateway.py` — replace `hasattr()`-only checks with actual behavior tests (fork RNG independence, replay/commit equivalence, capability+rate-limit+field-filtering end-to-end).
13. Add `tests/conftest.py` (plan's directory layout expects it; only `sim/conftest.py` exists today).

## Phase 5 — Documentation & config cleanup (mechanical, low-risk)

14. README.md / Makefile: remove/replace stale `make up`/`make down` + local docker-compose/Jaeger/Prometheus instructions with the actual Grafana Cloud + Supabase setup.
15. Standardize UUID version to **UUIDv7** (time-ordered, matches architecture.md's documented intent) across `docs/interfaces.md`, event/command ID generation in `engine.py` (currently a mix of `uuid4`/`uuid5`), and doc references.
16. Refresh `docs/architecture.md` status table — Gateway and Observability are implemented, not "Pending."
17. Update `docs/chrono_dag.md` / `docs/interfaces.md` `create_checkpoint()` signature to match the actual (larger) signature in code.
18. Drop unused `simpy` dependency from `pyproject.toml`; fix `sim/scheduler/env.py`'s docstring (it doesn't wrap simpy — it's a `heapq`-based priority queue).
19. Create `baselines/hashes.json` seed file so `scripts/regression_test.py` / `make regress` has something to compare against.
20. Add the missing `sim.gateway` isolation contract to `pyproject.toml`'s `[tool.importlinter]` config (currently only core/population/chrono are covered).
21. Have `scripts/download_data.py`'s `transactionsTypes.csv` actually feed into `sim/population/calibration.py` (currently downloaded/validated but never parsed).
22. Fix `initial_plan.md`/docs merchant-action mismatch: plan says "PAYOUT" (doesn't exist), code uses `SETTLEMENT`; and replace `behaviour.py`'s hardcoded 5% merchant-action probability with real temporal-model-driven rates per the plan.

## Explicitly deferred (documented only, no code change)

- **Gateway**: LangGraph adapter stays a stub (`NotImplementedError`), rate limiter stays counter-based (not token-bucket), `register_tool()` signature mismatch vs. protocol — all three will be called out accurately in `docs/interfaces.md` and `docs/architecture.md` as known gaps rather than fixed now.

---

**Suggested execution order**: Phase 1 (correctness) → Phase 2 (CLI) → Phase 4 (tests, so Phase 1/2 changes are covered) → Phase 3 (observability) → Phase 5 (docs/cleanup last, since earlier phases will shift some of what needs documenting).

---

## Post-implementation notes

All 5 phases were implemented and verified (61 tests passing, 5/5 import-linter contracts kept, mypy/ruff error counts unchanged or reduced vs. baseline). Two significant correctness bugs were discovered **during Phase 4** that weren't visible in the original audit, because until Phase 1 wired the engine to persist events, no financial command had ever actually executed end-to-end in any test or run:

1. **`PopulationManager.create_population()` never created real engine accounts** — it only built local `AgentEntity` records, so `world_view.accounts` was always empty and `propose_actions()` always returned `[]`. A "full simulation" produced scheduled steps but zero financial events. Fixed by adding `WorldEngine.create_account()` (genesis events through the same Emit→Append→Apply pipeline as `execute_command()`) and wiring population to call it.
2. **All `tx_id` generation used `uuid.uuid4()`** (os.urandom-backed, non-deterministic) across every transaction handler in `engine.py`. This directly broke the "identical seed → identical state hash" guarantee, but was invisible because no real transactions ever ran (see #1). Fixed with a deterministic `_next_tx_id()` (same `uuid5` derivation pattern as `_next_event_id()`).

Also found during Phase 4: **`contract_test_*.py` files were never collected by pytest** — they don't match the default `test_*.py`/`*_test.py` glob, so `make test-contract`'s `-k contract_test` filter was running against an empty collected set. Fixed via `python_files` in `pyproject.toml`. One contract test (`test_import_isolation`) was also broken-by-design (checked global `sys.modules`, which is polluted by test run order) — replaced with a static AST-based check.

Also found: event scheduling grows **superlinearly** with simulation duration at fixed population size (200 users: 6h→~6k steps, 24h→~800k steps). This smells like a bug in the interarrival/temporal model and is worth a dedicated investigation — not fixed here, kept test durations short to avoid it.

Not fixed (explicitly deferred per earlier decision): LangGraph adapter stub, counter-based (not token-bucket) rate limiter, `register_tool()` signature mismatch vs. its own protocol — all in `sim/gateway/`.
