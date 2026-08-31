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

    # Fraud detection is opt-in. Off, the engine emits exactly what it emitted
    # before the risk seam existed, so every replay, determinism and state-hash
    # guarantee is untouched by default.
    enable_risk: bool = False
    # Where the trained card model lives. When the file is absent the card rail
    # runs in its untrained state and says so, rather than guessing.
    card_model_path: Path = Path("models/card.pt")
    # Where scored traffic is appended, for training the next model. Unset means
    # no collection.
    traffic_log: Path | None = None

    config_file: Path = Path("config.yaml")

    model_config = {"env_prefix": "FINSIM_", "env_file": ".env", "extra": "ignore"}

    @classmethod
    def from_yaml(cls, path: Path | None = None) -> SimConfig:
        if path and path.exists():
            with open(path) as f:
                data = yaml.safe_load(f)
            return cls(**data)
        return cls()
