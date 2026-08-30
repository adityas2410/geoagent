"""Bounded native-Gemini fallback for ADK model calls."""

from __future__ import annotations

import copy
import logging
from contextvars import ContextVar
from contextvars import Token
from functools import cached_property
from typing import Any
from typing import AsyncGenerator
from typing import Awaitable
from typing import Callable

from google.adk.models.base_llm import BaseLlm
from google.adk.models.google_llm import Gemini
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.genai import types
from google.genai.errors import APIError
from pydantic import Field
from pydantic import model_validator


logger = logging.getLogger("geoagent.model_fallback")

TRANSIENT_MODEL_ERROR_CODES = frozenset({404, 429, 500, 502, 503, 504})
_ACTIVE_RUN_METRICS: ContextVar[dict[str, Any] | None] = ContextVar(
    "geoagent_active_run_metrics", default=None
)
_ACTIVE_RUN_ACTIVITY: ContextVar[
    Callable[[str, str, str], Awaitable[None]] | None
] = ContextVar("geoagent_active_run_activity", default=None)
_AGENT_TOOL_NAMES = {
    "mission_manager": frozenset(
        {
            "load_mission_state",
            "request_clarification",
            "request_objective_decision",
            "publish_plan",
        }
    ),
    "organizational_data_agent": frozenset(
        {"list_authorized_sources", "inspect_source_schema", "query_source"}
    ),
    "geospatial_intelligence_agent": frozenset(
        {
            "geocode_locations",
            "search_places",
            "compute_routes",
            "compute_route_matrix",
            "get_weather_context",
            "inspect_roads",
        }
    ),
    "planning_validation_agent": frozenset(
        {
            "normalize_operational_rules",
            "compose_resources",
            "optimize_assignments",
            "calculate_plan_metrics",
            "validate_plan",
        }
    ),
}


def activate_run_metrics(metrics: dict[str, Any]) -> Token[dict[str, Any] | None]:
    """Attach one Mission run's mutable counters to this async execution context."""
    return _ACTIVE_RUN_METRICS.set(metrics)


def reset_run_metrics(token: Token[dict[str, Any] | None]) -> None:
    """Remove the current Mission counters after its isolated run finishes."""
    _ACTIVE_RUN_METRICS.reset(token)


def activate_run_activity(
    sink: Callable[[str, str, str], Awaitable[None]],
) -> Token[Callable[[str, str, str], Awaitable[None]] | None]:
    """Attach a prompt-free lifecycle event sink to one Mission execution."""
    return _ACTIVE_RUN_ACTIVITY.set(sink)


def reset_run_activity(
    token: Token[Callable[[str, str, str], Awaitable[None]] | None],
) -> None:
    """Remove the current Mission activity sink after execution ends."""
    _ACTIVE_RUN_ACTIVITY.reset(token)


async def _emit_model_activity(llm_request: LlmRequest, model_name: str, phase: str) -> None:
    sink = _ACTIVE_RUN_ACTIVITY.get()
    if sink is not None:
        await sink(_agent_name_for_request(llm_request), model_name, phase)


def _agent_name_for_request(llm_request: LlmRequest) -> str:
    tool_names = set(llm_request.tools_dict)
    for agent_name, agent_tool_names in _AGENT_TOOL_NAMES.items():
        if tool_names & agent_tool_names:
            return agent_name
    return "unknown_agent"


def _record_llm_request(llm_request: LlmRequest, *, fallback: bool) -> None:
    """Count one physical Gemini API request without retaining request content."""
    metrics = _ACTIVE_RUN_METRICS.get()
    if metrics is None:
        return
    metrics["llm_requests"] = int(metrics.get("llm_requests", 0)) + 1
    if fallback:
        metrics["fallback_requests"] = int(metrics.get("fallback_requests", 0)) + 1
    by_agent = metrics.setdefault("llm_requests_by_agent", {})
    agent_name = _agent_name_for_request(llm_request)
    by_agent[agent_name] = int(by_agent.get(agent_name, 0)) + 1
    by_model = metrics.setdefault("model_requests", {})
    model_name = llm_request.model or "unknown_model"
    by_model[model_name] = int(by_model.get(model_name, 0)) + 1


def _record_usage_metadata(response: LlmResponse) -> None:
    """Sum token metadata returned by Gemini; never inspect response content."""
    metrics = _ACTIVE_RUN_METRICS.get()
    usage = response.usage_metadata
    if metrics is None or usage is None or response.partial:
        return
    fields = {
        "input_tokens": "prompt_token_count",
        "output_tokens": "candidates_token_count",
        "thinking_tokens": "thoughts_token_count",
        "cached_input_tokens": "cached_content_token_count",
        "tool_use_prompt_tokens": "tool_use_prompt_token_count",
        "total_tokens": "total_token_count",
    }
    for metric_name, usage_field in fields.items():
        value = getattr(usage, usage_field, None)
        if isinstance(value, int):
            metrics[metric_name] = int(metrics.get(metric_name, 0)) + value


def _record_model_failure(model_name: str) -> None:
    """Record a failed Gemini request without retaining its error payload."""
    metrics = _ACTIVE_RUN_METRICS.get()
    if metrics is None:
        return
    failures = metrics.setdefault("model_failures", {})
    failures[model_name] = int(failures.get(model_name, 0)) + 1


def _copy_request_for_model(llm_request: LlmRequest) -> LlmRequest:
    """Copy fields Gemini may mutate without copying ADK tool locks."""
    candidate = llm_request.model_copy(deep=False)
    candidate.contents = copy.deepcopy(llm_request.contents)
    candidate.config = copy.deepcopy(llm_request.config)
    candidate.live_connect_config = copy.deepcopy(llm_request.live_connect_config)
    candidate.tools_dict = dict(llm_request.tools_dict)
    candidate.cache_metadata = copy.deepcopy(llm_request.cache_metadata)
    return candidate


class FallbackGemini(BaseLlm):
    """Try an ordered list of native Gemini models for transient failures."""

    fallback_models: tuple[str, ...] = Field(default_factory=tuple)
    retry_options: types.HttpRetryOptions = Field(
        default_factory=lambda: types.HttpRetryOptions(attempts=1)
    )
    @model_validator(mode="after")
    def validate_models(self) -> "FallbackGemini":
        candidates = self.model_names
        if any(not name.startswith("gemini-") for name in candidates):
            raise ValueError("FallbackGemini accepts only native Gemini model IDs")
        if len(set(candidates)) != len(candidates):
            raise ValueError("FallbackGemini model IDs must be unique")
        return self

    @property
    def model_names(self) -> tuple[str, ...]:
        """Return the primary model followed by ordered fallbacks."""
        return (self.model, *self.fallback_models)

    @cached_property
    def delegates(self) -> tuple[Gemini, ...]:
        """Build one native ADK Gemini client per configured model."""
        return tuple(
            Gemini(model=name, retry_options=self.retry_options)
            for name in self.model_names
        )

    async def generate_content_async(
        self, llm_request: LlmRequest, stream: bool = False
    ) -> AsyncGenerator[LlmResponse, None]:
        """Use the next model only when the current one has a transient failure."""
        # Gemini preprocessing mutates request contents and config. ADK tool
        # objects can contain thread locks, so they must remain shallow-copied.
        candidate_requests = [
            _copy_request_for_model(llm_request) for _ in self.delegates
        ]
        for index, (delegate, candidate_request) in enumerate(
            zip(self.delegates, candidate_requests, strict=True)
        ):
            candidate_request.model = delegate.model
            emitted_response = False
            usage_recorded = False
            try:
                _record_llm_request(candidate_request, fallback=index > 0)
                await _emit_model_activity(candidate_request, delegate.model, "started")
                async for response in delegate.generate_content_async(
                    candidate_request, stream=stream
                ):
                    emitted_response = True
                    if not usage_recorded and not response.partial:
                        _record_usage_metadata(response)
                        usage_recorded = response.usage_metadata is not None
                    yield response
                await _emit_model_activity(candidate_request, delegate.model, "completed")
                if index:
                    logger.warning(
                        "Gemini fallback succeeded primary=%s selected=%s",
                        self.model,
                        delegate.model,
                    )
                return
            except APIError as error:
                _record_model_failure(delegate.model)
                await _emit_model_activity(candidate_request, delegate.model, "failed")
                has_fallback = index + 1 < len(self.delegates)
                if (
                    emitted_response
                    or error.code not in TRANSIENT_MODEL_ERROR_CODES
                    or not has_fallback
                ):
                    raise
                next_model = self.delegates[index + 1].model
                logger.warning(
                    "Gemini model unavailable model=%s status=%s fallback=%s",
                    delegate.model,
                    error.code,
                    next_model,
                )
