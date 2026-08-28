"""Bounded native-Gemini fallback for ADK model calls."""

from __future__ import annotations

import copy
import logging
from functools import cached_property
from typing import AsyncGenerator

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
            try:
                async for response in delegate.generate_content_async(
                    candidate_request, stream=stream
                ):
                    emitted_response = True
                    yield response
                if index:
                    logger.warning(
                        "Gemini fallback succeeded primary=%s selected=%s",
                        self.model,
                        delegate.model,
                    )
                return
            except APIError as error:
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
