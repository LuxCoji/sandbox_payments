# Architectural Suggestions & Future Capabilities

This document outlines strategic suggestions for maximizing the utility of the `ChronoDAG` branching system, particularly regarding Red Team agent simulations and simulation realism.

## 1. Running Live Simulations on Forked Branches
Currently, when a branch is forked from `main`, the discrete-event scheduler's queue (the organic background traffic) does not carry over. 
**Suggestion:** We must prioritize restoring the `SimulationEnv._queue` upon branch checkout so that organic background traffic continues to run on forked branches.
* **Why:** This ensures the Red Team agent attacks a "live" world rather than a frozen one. It introduces necessary organic noise, race conditions, and dynamic liquidity constraints. Without this, attacks succeed in a sterile environment and may not generalize to real-world conditions where the attacker must blend in with normal traffic.

## 2. Maintaining `main` as a Pristine Counterfactual
**Suggestion:** The `main` branch should always remain strictly read-only for adversarial agents.
* **Why:** Keeping `main` unaffected provides a perfect "control group". When an attack occurs on a forked branch, we can use the `diff_branches` tool against `main` to see the exact blast radius of the attack (e.g., precise monetary losses, tripped flags) isolated completely from natural economic noise.

## 3. Extended Uses for Forked Branches
Beyond simply storing successful attacks, branches provide a multiverse of simulation possibilities:
* **Blue Team Training (Defense Tuning):** Once a Red Team commits a successful attack to a branch, we can fork *that* compromised branch and deploy a "Blue Team" agent or a new static defense rule to see if it can detect or freeze the attack retroactively.
* **Multi-Adversary Tournaments:** Fork multiple branches from the exact same `main` checkpoint and deploy different Red Team agents (e.g., using different LLMs, prompts, or constraints) on each. This allows for objective benchmarking of which adversary is most effective under identical starting conditions.
* **Macroeconomic Stress Testing:** Fork `main` into different economic states (e.g., a "recession" branch where balances drop, or a "boom" branch). Unleash the Red Team on both to observe how adversarial strategies pivot based on macroeconomic conditions.

## 4. Re-evaluating the "Save-Scumming" Advantage
**Suggestion:** As the platform matures, we should evaluate restricting the Red Team's ability to arbitrarily fork branches for its own exploration.
* **Why:** Currently, the agent can use branches as a "scratch multiverse"—trying an attack, and if caught, abandoning the branch to try again. While useful for stress-testing the absolute limits of the system in Phase 1, this gives the attacker an unrealistic "omniscient" advantage since real-world fraud is a one-way door with permanent consequences.
