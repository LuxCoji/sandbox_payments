"""LiteLLM Router wrapper — in-process, multi-provider/multi-account rotation
pool for the red-team persona's LLM calls.

One `litellm.Router` model_name ("redteam-agent" by default) backed by N
deployments across providers/accounts (see `providers.yaml`). Lockstep
interaction model (docs/redteam_agent_design.md §1): one call here decides
exactly one next action, given the current world view and the outcome of the
agent's previous action — not a multi-step plan.
"""
from __future__ import annotations

import dataclasses
import json
import logging
import os
import time
from dataclasses import dataclass

import litellm
from litellm import Router
from litellm.types.router import RouterRateLimitError, RouterRateLimitErrorBasic
from opentelemetry import trace as otel_trace

from agents.redteam.config import RedTeamConfig, load_provider_deployments, resolve_api_key
from sim.observability import traced

logger = logging.getLogger(__name__)

_otel_callback_registered = False


def _maybe_register_otel_callback(config: RedTeamConfig) -> None:
    """Route litellm's own request/response/routing telemetry — which
    provider a call landed on, latency, retries, token usage, and (with
    capture_message_content enabled) the prompt/response content including
    the agent's parsed "reasoning" field — into the same OTLP pipeline
    sim.observability.setup_tracing() already points at Grafana Cloud, not
    the terminal. Per-turn "thinking" is visible there, not printed anywhere.

    Gated on config.enable_otel_tracing (default True), not on
    OTEL_EXPORTER_OTLP_ENDPOINT's presence — importing litellm loads .env as
    a side effect (a known litellm quirk) which sets that env var in every
    process regardless of test intent, so its absence isn't a safe signal.
    Test fixtures explicitly pass enable_otel_tracing=False.
    """
    global _otel_callback_registered
    if _otel_callback_registered or not config.enable_otel_tracing:
        return
    if not os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT"):
        logger.info("enable_otel_tracing is True but OTEL_EXPORTER_OTLP_ENDPOINT is unset — skipping")
        return

    from litellm.integrations.opentelemetry import OpenTelemetry, OpenTelemetryConfig

    otel_config = OpenTelemetryConfig.from_env()
    otel_config.capture_message_content = "SPAN_ONLY"
    litellm.callbacks.append(OpenTelemetry(config=otel_config))
    _otel_callback_registered = True

# Requesting a plain function call/JSON response from the LLM, not a full
# tool-calling round trip through the provider's function-calling API —
# providers/models in the free-tier pool vary widely in tool-call support,
# JSON-mode-via-prompt is the lowest common denominator across all four.
_RESPONSE_INSTRUCTIONS = """
Respond with a single JSON object and nothing else, in this exact shape:
{"tool_name": "<tool>", "parameters": {...}, "reasoning": "<reasoning>"}

"reasoning" is not a caption for the action — it is the only part of your
thinking that survives to future turns (it is replayed back to you in your
step history), so it is where your strategy lives. State what step of your
pattern this is and what you expect it to establish. Do not restate what
the tool does or narrate the world state back.
"""


@dataclass(frozen=True)
class NextAction:
    """The single action the agent decided on this lockstep turn."""

    tool_name: str
    parameters: dict[str, object]
    reasoning: str
    # Which pool deployment actually served this call, and how long it took
    # — routing/latency telemetry for observability (see
    # docs/redteam_agent_design.md §8). None when unavailable (e.g. a
    # hand-built NextAction in a test).
    provider_model: str | None = None
    latency_ms: float | None = None


class ProviderPoolExhausted(Exception):
    """Raised only if retries are exhausted past retry_backoff_max_s ceiling
    without litellm.Router itself recovering — see decide_next_action."""


def build_router(config: RedTeamConfig | None = None) -> Router:
    """Build a litellm.Router from providers.yaml.

    Every deployment shares model_name=config.model_name so Router's own
    load-balancing/failover picks among them per config.routing_strategy —
    this is what makes the pool a single rotation target instead of N
    separately-addressed models.

    Deployments whose api_key_env isn't set in the environment are skipped
    with a warning rather than failing the whole build — the pool is meant
    to keep working with however many providers are actually configured,
    not require every entry in providers.yaml to have a key before the
    harness can run at all.
    """
    config = config or RedTeamConfig()
    _maybe_register_otel_callback(config)
    deployments = load_provider_deployments(config)
    if not deployments:
        raise RuntimeError(f"No deployments found in {config.providers_file}")

    model_list = []
    for d in deployments:
        try:
            api_key = resolve_api_key(d)
        except RuntimeError as exc:
            logger.warning("Skipping deployment %s (%s): %s", d.provider, d.litellm_model, exc)
            continue
        litellm_params: dict[str, object] = {"model": d.litellm_model, "api_key": api_key}
        if d.api_base:
            litellm_params["api_base"] = d.api_base
        if d.rpm:
            litellm_params["rpm"] = d.rpm
        if d.max_tokens:
            litellm_params["max_tokens"] = d.max_tokens
        model_list.append({"model_name": config.model_name, "litellm_params": litellm_params})

    if not model_list:
        raise RuntimeError(
            f"No deployments in {config.providers_file} have a configured API key — "
            "set at least one api_key_env in .env."
        )

    return Router(model_list=model_list, routing_strategy=config.routing_strategy, num_retries=0)


@traced("RedTeam.decide_next_action")
def decide_next_action(
    router: Router,
    config: RedTeamConfig,
    user_message: str,
    persona_prompt: str,
) -> NextAction:
    """One lockstep LLM call: given the fully-assembled turn context,
    decide exactly one next tool call.

    `user_message` arrives already rendered — this function does not
    compose it. Prompt composition lives in agents/redteam/context.py
    (TurnContext.render), which owns block selection and ordering; this
    module owns routing, retry/backoff, and response parsing. They were
    previously entangled: the harness built a "world_summary" string and
    this function separately decided where the history and last-outcome
    blocks went around it, so no single place was responsible for what
    the model actually saw. See context.py's module docstring for the
    defects that split caused.

    On full-pool exhaustion (every deployment rate-limited), block and retry
    with exponential backoff up to retry_backoff_max_s per attempt — per the
    confirmed exhaustion behavior (block-and-retry, not skip-session or
    hard-fail). litellm.Router's own num_retries is left at 0 here (see
    build_router) so backoff/retry policy lives in one place, this function,
    rather than being split across Router's retry config and a wrapper.

    The decided action's tool_name/reasoning are attached as attributes on
    this function's own OTel span (via @traced) — visible in Grafana Cloud
    alongside litellm's own routing/latency spans (see
    _maybe_register_otel_callback), never printed to the terminal.
    """
    messages = [
        {"role": "system", "content": persona_prompt + _RESPONSE_INSTRUCTIONS},
        {"role": "user", "content": user_message},
    ]

    backoff_s = config.retry_backoff_base_s
    attempt = 0
    parse_failures = 0
    while True:
        attempt += 1
        try:
            call_started = time.monotonic()
            response = router.completion(model=config.model_name, messages=messages)
            latency_ms = (time.monotonic() - call_started) * 1000
            content = response.choices[0].message.content
            try:
                parsed = _parse_next_action(content)
            except (ValueError, KeyError, TypeError) as exc:
                # Not a rate-limit/connectivity failure — a free-tier model
                # returned content that isn't a well-formed {"tool_name":
                # ...} object at all (prose refusal, truncated output,
                # missing "tool_name" key). Previously uncaught: this
                # propagated straight out of decide_next_action and crashed
                # the whole session on a single bad turn — losing every step
                # already taken (the end-of-session checkpoint only runs
                # after the loop finishes normally) over what's usually a
                # one-off formatting slip from a small model, not a real
                # failure. Bounded retry (config.max_parse_retries, separate
                # counter from the rate-limit backoff above) gives the
                # Router a chance to land the retry on a different, better-
                # behaved deployment before giving up for real.
                parse_failures += 1
                logger.warning(
                    "Unparseable LLM response (parse attempt %d/%d): %s",
                    parse_failures, config.max_parse_retries, exc,
                )
                if parse_failures >= config.max_parse_retries:
                    raise
                continue
            action = dataclasses.replace(
                parsed, provider_model=getattr(response, "model", None), latency_ms=latency_ms
            )
            span = otel_trace.get_current_span()
            span.set_attribute("redteam.tool_name", action.tool_name)
            span.set_attribute("redteam.reasoning", action.reasoning)
            return action
        except (RouterRateLimitError, RouterRateLimitErrorBasic) as exc:
            # "No deployments available for selected model" — every
            # deployment in the pool is in litellm's cooldown list at
            # once. This is the whole-pool-exhausted case §7 specifies
            # block-and-retry for, but it was crashing the session
            # instead: RouterRateLimitError subclasses ValueError, NOT
            # litellm.RateLimitError, so the handler below never saw it
            # and it propagated straight out of decide_next_action.
            #
            # Unlike a provider's own 429, litellm tells us exactly how
            # long the cooldown has left, so honour that rather than
            # guessing with the doubling backoff — retrying earlier just
            # reproduces the same error. Floored at 1s and capped at
            # retry_backoff_max_s so a pathological cooldown_time can't
            # stall the session indefinitely in one sleep; if the cap is
            # short of the real cooldown the next attempt simply sleeps
            # again, which converges on its own.
            cooldown_s = float(getattr(exc, "cooldown_time", 0.0) or 0.0)
            wait_s = min(max(cooldown_s, 1.0), config.retry_backoff_max_s)
            logger.warning(
                "Whole provider pool in cooldown (attempt %d), waiting %.1fs: %s",
                attempt, wait_s, exc,
            )
            time.sleep(wait_s)
        except litellm.RateLimitError:
            logger.warning(
                "Provider pool exhausted (attempt %d), backing off %.1fs", attempt, backoff_s
            )
            time.sleep(backoff_s)
            backoff_s = min(backoff_s * 2, config.retry_backoff_max_s)
        except (litellm.APIConnectionError, litellm.Timeout) as exc:
            logger.warning("Transient provider error (attempt %d): %s", attempt, exc)
            time.sleep(backoff_s)
            backoff_s = min(backoff_s * 2, config.retry_backoff_max_s)


def _parse_next_action(content: str | None) -> NextAction:
    if not content:
        raise ValueError("LLM returned empty response")
    # Models in the free-tier pool sometimes wrap JSON in prose or code
    # fences despite instructions — extract the first {...} block.
    start, end = content.find("{"), content.rfind("}")
    if start == -1 or end == -1:
        raise ValueError(f"No JSON object found in LLM response: {content!r}")
    data = json.loads(content[start : end + 1])
    return NextAction(
        tool_name=data["tool_name"],
        parameters=data.get("parameters", {}),
        reasoning=data.get("reasoning", ""),
    )
