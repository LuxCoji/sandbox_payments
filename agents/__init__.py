"""Harness code for AI-driven agents that interact with FinSim via `sim.gateway`.

This package sits outside `sim`'s import-linter root_package on purpose: it is
free to import `sim.*` (mainly `sim.gateway.interfaces`, `sim.chrono.interfaces`,
and `sim.main`), but `sim` must never import `agents` — enforced by the "Sim
does not import the red-team harness" contract in pyproject.toml.
"""
