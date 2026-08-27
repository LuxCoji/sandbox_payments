"""Thin wrapper around the real CLI in sim/main.py.

Kept for convenience (`python scripts/run_simulation.py ...`) and backward
compatibility; the composition-root CLI lives in sim/main.py per the plan
(`python -m sim.main <command>`).
"""
from sim.main import main

if __name__ == "__main__":
    main()
