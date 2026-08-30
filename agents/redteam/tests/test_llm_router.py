"""Unit tests for llm_router.py — mocked litellm calls, no real network access."""
from __future__ import annotations

import json
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import patch

import litellm
import pytest

if TYPE_CHECKING:
    from pathlib import Path

from agents.redteam.config import RedTeamConfig
from agents.redteam.llm_router import NextAction, _parse_next_action, build_router, decide_next_action


@pytest.fixture
def fixture_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> RedTeamConfig:
    providers_file = tmp_path / "providers.yaml"
    providers_file.write_text(
        """
deployments:
  - provider: groq
    litellm_model: groq/llama-3.3-70b-versatile
    api_key_env: TEST_GROQ_KEY
    rpm: 30
  - provider: openrouter
    litellm_model: openrouter/meta-llama/llama-3.3-70b-instruct:free
    api_key_env: TEST_OPENROUTER_KEY
    rpm: 20
"""
    )
    monkeypatch.setenv("TEST_GROQ_KEY", "fake-groq-key")
    monkeypatch.setenv("TEST_OPENROUTER_KEY", "fake-openrouter-key")
    return RedTeamConfig(
        providers_file=providers_file, retry_backoff_base_s=0.01, retry_backoff_max_s=0.05,
        enable_otel_tracing=False,
    )


def test_build_router_uses_one_model_name_for_all_deployments(fixture_config: RedTeamConfig) -> None:
    router = build_router(fixture_config)
    model_names = {entry["model_name"] for entry in router.get_model_list()}
    assert model_names == {fixture_config.model_name}
    assert len(router.get_model_list()) == 2


def test_build_router_skips_deployments_missing_their_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    providers_file = tmp_path / "providers.yaml"
    providers_file.write_text(
        "deployments:\n"
        "  - provider: groq\n"
        "    litellm_model: groq/llama-3.3-70b-versatile\n"
        "    api_key_env: TEST_GROQ_KEY\n"
        "  - provider: openrouter\n"
        "    litellm_model: openrouter/meta-llama/llama-3.3-70b-instruct:free\n"
        "    api_key_env: TEST_OPENROUTER_KEY_UNSET\n"  # deliberately never set
    )
    monkeypatch.setenv("TEST_GROQ_KEY", "fake-groq-key")
    config = RedTeamConfig(providers_file=providers_file, enable_otel_tracing=False)

    router = build_router(config)

    model_list = router.get_model_list()
    assert len(model_list) == 1
    assert model_list[0]["litellm_params"]["model"] == "groq/llama-3.3-70b-versatile"


def test_build_router_passes_through_per_deployment_max_tokens(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ProviderDeployment.max_tokens (e.g. the nvidia nemotron reasoning
    deployment in providers.yaml — its <think> trace shares the completion-
    token budget with the JSON output) must reach litellm_params, not be
    silently dropped the way rpm/api_base would be if the same "if truthy"
    passthrough were forgotten for a new field.
    """
    providers_file = tmp_path / "providers.yaml"
    providers_file.write_text(
        "deployments:\n"
        "  - provider: nvidia\n"
        "    litellm_model: nvidia_nim/nvidia/nemotron-3-super-120b-a12b\n"
        "    api_key_env: TEST_NVIDIA_KEY\n"
        "    max_tokens: 16384\n"
    )
    monkeypatch.setenv("TEST_NVIDIA_KEY", "fake-nvidia-key")
    config = RedTeamConfig(providers_file=providers_file, enable_otel_tracing=False)

    router = build_router(config)

    assert router.get_model_list()[0]["litellm_params"]["max_tokens"] == 16384


def test_build_router_raises_if_no_deployment_has_a_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    providers_file = tmp_path / "providers.yaml"
    providers_file.write_text(
        "deployments:\n"
        "  - provider: groq\n"
        "    litellm_model: groq/llama-3.3-70b-versatile\n"
        "    api_key_env: TEST_GROQ_KEY_UNSET\n"
    )
    monkeypatch.delenv("TEST_GROQ_KEY_UNSET", raising=False)
    config = RedTeamConfig(providers_file=providers_file, enable_otel_tracing=False)

    with pytest.raises(RuntimeError, match="No deployments"):
        build_router(config)


def test_parse_next_action_plain_json() -> None:
    content = json.dumps({"tool_name": "transfer_funds", "parameters": {"amount": 10}, "reasoning": "test"})
    action = _parse_next_action(content)
    assert action == NextAction("transfer_funds", {"amount": 10}, "test")


def test_parse_next_action_wrapped_in_prose() -> None:
    content = 'Sure, here you go:\n```json\n{"tool_name": "inspect_account", "parameters": {}}\n```\nDone.'
    action = _parse_next_action(content)
    assert action.tool_name == "inspect_account"
    assert action.reasoning == ""


def test_parse_next_action_no_json_raises() -> None:
    with pytest.raises(ValueError, match="No JSON object"):
        _parse_next_action("I refuse to answer in JSON.")


def test_parse_next_action_empty_raises() -> None:
    with pytest.raises(ValueError, match="empty response"):
        _parse_next_action(None)


def _fake_response(tool_name: str) -> SimpleNamespace:
    content = json.dumps({"tool_name": tool_name, "parameters": {}, "reasoning": "r"})
    message = SimpleNamespace(content=content)
    choice = SimpleNamespace(message=message)
    return SimpleNamespace(choices=[choice])


def test_decide_next_action_succeeds_first_try(fixture_config: RedTeamConfig) -> None:
    router = build_router(fixture_config)
    with patch.object(router, "completion", return_value=_fake_response("make_payment")) as mock_completion:
        action = decide_next_action(router, fixture_config, "world view", "persona prompt")
    assert action.tool_name == "make_payment"
    mock_completion.assert_called_once()


def test_decide_next_action_retries_on_rate_limit_then_succeeds(fixture_config: RedTeamConfig) -> None:
    router = build_router(fixture_config)
    side_effects = [
        litellm.RateLimitError("rate limited", llm_provider="groq", model="groq/llama-3.3-70b-versatile"),
        _fake_response("transfer_funds"),
    ]
    with patch.object(router, "completion", side_effect=side_effects) as mock_completion:
        action = decide_next_action(router, fixture_config, "world view", "persona prompt")
    assert action.tool_name == "transfer_funds"
    assert mock_completion.call_count == 2


def _fake_response_content(content: str | None) -> SimpleNamespace:
    message = SimpleNamespace(content=content)
    choice = SimpleNamespace(message=message)
    return SimpleNamespace(choices=[choice])


def test_decide_next_action_retries_on_unparseable_response_then_succeeds(
    fixture_config: RedTeamConfig,
) -> None:
    """A free-tier model returning prose instead of JSON is a formatting
    failure, not a rate-limit failure — previously this propagated the raw
    ValueError straight out of decide_next_action and crashed the whole
    session on a single bad turn (see the fix's comment in llm_router.py).
    It must be retried (bounded, separately from the rate-limit backoff)
    instead.
    """
    router = build_router(fixture_config)
    side_effects = [
        _fake_response_content("I'm not going to do that."),
        _fake_response("inspect_account"),
    ]
    with patch.object(router, "completion", side_effect=side_effects) as mock_completion:
        action = decide_next_action(router, fixture_config, "world view", "persona prompt")
    assert action.tool_name == "inspect_account"
    assert mock_completion.call_count == 2


def test_decide_next_action_raises_after_max_parse_retries(fixture_config: RedTeamConfig) -> None:
    fixture_config = fixture_config.model_copy(update={"max_parse_retries": 2})
    router = build_router(fixture_config)
    with patch.object(
        router, "completion", return_value=_fake_response_content("still not JSON")
    ) as mock_completion, pytest.raises(ValueError, match="No JSON object"):
        decide_next_action(router, fixture_config, "world view", "persona prompt")
    assert mock_completion.call_count == 2
