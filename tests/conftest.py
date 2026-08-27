"""Shared pytest fixtures for tests/integration/*.

(Named in the plan's directory layout; previously missing — only
sim/conftest.py existed for the unit-test tree.)
"""
from __future__ import annotations

from pathlib import Path

import pytest

from sim.population.calibration import calibrate_from_csv
from sim.population.interfaces import CalibratedParams

DATA_DIR = Path(__file__).resolve().parent.parent / "data" / "paysim"


@pytest.fixture
def data_dir() -> Path:
    return DATA_DIR


@pytest.fixture
def calibrated_params() -> CalibratedParams:
    try:
        return calibrate_from_csv(DATA_DIR)
    except FileNotFoundError:
        return CalibratedParams({}, (), {}, {}, {})
