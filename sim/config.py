"""Simulation configuration via Pydantic Settings."""
from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic_settings import BaseSettings


class SimConfig(BaseSettings):
    """Top-level simulation configuration."""

    seed: int = 42
    num_users: int = 1000
    num_merchants: int = 50
    sim_duration_days: int = 30

    chrono_backend: Literal["memory", "postgres"] = "memory"
    db_url: str | None = None

    otel_endpoint: str | None = None
    prometheus_port: int = 9090

    config_file: Path = Path("config.yaml")

    model_config = {"env_prefix": "FINSIM_", "env_file": ".env"}

    @classmethod
    def from_yaml(cls, path: Path | None = None) -> SimConfig:
        if path and path.exists():
            with open(path) as f:
                data = yaml.safe_load(f)
            return cls(**data)
        return cls()
