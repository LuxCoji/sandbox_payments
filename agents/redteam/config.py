"""Red-team harness configuration.

Mirrors the pattern in `sim/config.py` (pydantic-settings, `.env`-backed) but
lives outside `sim` — this is harness configuration, not simulation
configuration, and `sim` must never import it (see the "Sim does not import
the red-team harness" import-linter contract in pyproject.toml).
"""
from __future__ import annotations

import os
from pathlib import Path

import yaml
from pydantic import BaseModel
from pydantic_settings import BaseSettings, SettingsConfigDict


class ProviderDeployment(BaseModel):
    """One entry in providers.yaml — one account/key against one provider."""

    provider: str
    litellm_model: str
    api_key_env: str
    api_base: str | None = None
    rpm: int | None = None


class RedTeamConfig(BaseSettings):
    """Top-level red-team harness configuration."""

    providers_file: Path = Path("agents/redteam/providers.yaml")

    # Single litellm.Router model_name — one persona rotating across the
    # whole provider/account pool, not one deployment per persona.
    model_name: str = "redteam-agent"
    routing_strategy: str = "simple-shuffle"

    # 2-4h default per docs/redteam_agent_design.md §2 (NOT 24h — see the
    # documented superlinear event-growth gotcha in CLAUDE.md).
    warmup_hours: float = 3.0

    session_max_plan_calls: int = 8
    retry_backoff_base_s: float = 2.0
    retry_backoff_max_s: float = 120.0

    model_config = SettingsConfigDict(env_prefix="FINSIM_REDTEAM_", env_file=".env", extra="ignore")


def load_provider_deployments(config: RedTeamConfig) -> list[ProviderDeployment]:
    """Parse providers.yaml into ProviderDeployment entries.

    Does not resolve api_key_env against os.environ here — that happens in
    llm_router.build_router, right before constructing the litellm.Router,
    so a missing key fails loudly at startup rather than silently here.
    """
    if not config.providers_file.exists():
        raise FileNotFoundError(
            f"providers.yaml not found at {config.providers_file} — see "
            "agents/redteam/providers.yaml for the expected format."
        )
    with open(config.providers_file) as f:
        raw = yaml.safe_load(f) or {}
    return [ProviderDeployment(**entry) for entry in raw.get("deployments", [])]


def resolve_api_key(deployment: ProviderDeployment) -> str:
    """Look up a deployment's API key from its configured env var.

    Raises a clear error naming the missing env var rather than letting
    litellm fail later with an opaque auth error.
    """
    value = os.environ.get(deployment.api_key_env)
    if not value:
        raise RuntimeError(
            f"Provider '{deployment.provider}' ({deployment.litellm_model}) "
            f"references env var {deployment.api_key_env!r}, which is unset. "
            "Add it to .env."
        )
    return value
