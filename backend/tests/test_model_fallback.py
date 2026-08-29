from __future__ import annotations

import asyncio
import sys
import unittest
from pathlib import Path
from typing import Callable


BACKEND_DIRECTORY = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_DIRECTORY))

from google.adk.models.llm_request import LlmRequest  # noqa: E402
from google.adk.models.llm_response import LlmResponse  # noqa: E402
from google.genai.errors import APIError  # noqa: E402

from geoagent.model_fallback import FallbackGemini  # noqa: E402


class StubGemini:
    def __init__(
        self,
        model: str,
        calls: list[str],
        *,
        error_code: int | None = None,
        emit_before_error: bool = False,
        on_call: Callable[[object], None] | None = None,
    ) -> None:
        self.model = model
        self.calls = calls
        self.error_code = error_code
        self.emit_before_error = emit_before_error
        self.on_call = on_call

    async def generate_content_async(self, request, stream=False):
        self.calls.append(request.model)
        if self.on_call:
            self.on_call(request)
        if self.emit_before_error:
            yield LlmResponse(partial=True)
        if self.error_code is not None:
            raise APIError(
                self.error_code,
                {"error": {"code": self.error_code, "status": "TEST"}},
            )
        yield LlmResponse(partial=False)


class FallbackGeminiTests(unittest.IsolatedAsyncioTestCase):
    async def test_timeout_uses_next_model(self) -> None:
        class StalledGemini(StubGemini):
            async def generate_content_async(self, request, stream=False):
                self.calls.append(request.model)
                await asyncio.sleep(1)
                yield LlmResponse(partial=False)

        calls: list[str] = []
        model = FallbackGemini(
            model="gemini-3.7-flash",
            fallback_models=("gemini-3.6-flash",),
            request_timeout_seconds=1,
        )
        model.__dict__["delegates"] = (
            StalledGemini("gemini-3.7-flash", calls),
            StubGemini("gemini-3.6-flash", calls),
        )

        responses = [
            response async for response in model.generate_content_async(LlmRequest())
        ]

        self.assertEqual(calls, ["gemini-3.7-flash", "gemini-3.6-flash"])
        self.assertEqual(len(responses), 1)

    async def test_prepares_all_request_copies_before_primary_mutates_tools(self) -> None:
        class SharedToolState:
            locked = False

            def __deepcopy__(self, memo):
                if self.locked:
                    raise TypeError("cannot copy after model preprocessing")
                return self

        calls: list[str] = []
        shared_state = SharedToolState()
        request = LlmRequest()
        request.__dict__["tools_dict"] = {"tool": shared_state}
        model = FallbackGemini(
            model="gemini-3.7-flash",
            fallback_models=("gemini-3.6-flash",),
        )
        model.__dict__["delegates"] = (
            StubGemini(
                "gemini-3.7-flash",
                calls,
                error_code=503,
                on_call=lambda _: setattr(shared_state, "locked", True),
            ),
            StubGemini("gemini-3.6-flash", calls),
        )

        responses = [
            response async for response in model.generate_content_async(request)
        ]

        self.assertEqual(calls, ["gemini-3.7-flash", "gemini-3.6-flash"])
        self.assertEqual(len(responses), 1)

    async def test_transient_error_uses_next_model(self) -> None:
        calls: list[str] = []
        model = FallbackGemini(
            model="gemini-3.7-flash",
            fallback_models=("gemini-3.6-flash", "gemini-3.5-flash"),
        )
        model.__dict__["delegates"] = (
            StubGemini("gemini-3.7-flash", calls, error_code=503),
            StubGemini("gemini-3.6-flash", calls),
            StubGemini("gemini-3.5-flash", calls),
        )

        responses = [
            response
            async for response in model.generate_content_async(LlmRequest())
        ]

        self.assertEqual(calls, ["gemini-3.7-flash", "gemini-3.6-flash"])
        self.assertEqual(len(responses), 1)

    async def test_non_transient_error_does_not_fallback(self) -> None:
        calls: list[str] = []
        model = FallbackGemini(
            model="gemini-3.7-flash",
            fallback_models=("gemini-3.6-flash",),
        )
        model.__dict__["delegates"] = (
            StubGemini("gemini-3.7-flash", calls, error_code=401),
            StubGemini("gemini-3.6-flash", calls),
        )

        with self.assertRaises(APIError):
            async for _ in model.generate_content_async(LlmRequest()):
                pass

        self.assertEqual(calls, ["gemini-3.7-flash"])

    async def test_streaming_error_after_output_does_not_restart(self) -> None:
        calls: list[str] = []
        model = FallbackGemini(
            model="gemini-3.7-flash",
            fallback_models=("gemini-3.6-flash",),
        )
        model.__dict__["delegates"] = (
            StubGemini(
                "gemini-3.7-flash",
                calls,
                error_code=503,
                emit_before_error=True,
            ),
            StubGemini("gemini-3.6-flash", calls),
        )

        responses = []
        with self.assertRaises(APIError):
            async for response in model.generate_content_async(
                LlmRequest(), stream=True
            ):
                responses.append(response)

        self.assertEqual(calls, ["gemini-3.7-flash"])
        self.assertEqual(len(responses), 1)

    def test_rejects_non_gemini_or_duplicate_models(self) -> None:
        with self.assertRaises(ValueError):
            FallbackGemini(model="other-model")
        with self.assertRaises(ValueError):
            FallbackGemini(
                model="gemini-3.7-flash",
                fallback_models=("gemini-3.7-flash",),
            )


if __name__ == "__main__":
    unittest.main()
