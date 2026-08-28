"""Configure local ADK activity logs and optional Google Cloud telemetry."""

from __future__ import annotations

import logging
import os
from pathlib import Path

from dotenv import load_dotenv


def configure_observability() -> None:
    """Configure observability before importing ADK agents or runners."""
    load_dotenv(Path(__file__).resolve().parents[1] / ".env")

    level_name = os.getenv("GEOAGENT_ADK_LOG_LEVEL", "INFO").upper()
    level = getattr(logging, level_name, None)
    if not isinstance(level, int):
        raise RuntimeError("GEOAGENT_ADK_LOG_LEVEL must be a valid logging level")
    handler = logging.StreamHandler()
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
    )
    for logger_name in ("google_adk", "geoagent"):
        configured_logger = logging.getLogger(logger_name)
        configured_logger.setLevel(level)
        if not configured_logger.handlers:
            configured_logger.addHandler(handler)
        configured_logger.propagate = False

    if os.getenv("GEOAGENT_OTEL_TO_CLOUD", "false").lower() not in {
        "1",
        "true",
        "yes",
    }:
        return

    os.environ.setdefault("OTEL_SERVICE_NAME", "geoagent")
    os.environ.setdefault(
        "OTEL_SEMCONV_STABILITY_OPT_IN", "gen_ai_latest_experimental"
    )
    os.environ.setdefault(
        "OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT", "EVENT_ONLY"
    )
    os.environ.setdefault("ADK_CAPTURE_MESSAGE_CONTENT_IN_SPANS", "false")

    from google.adk.telemetry.google_cloud import get_gcp_exporters
    from google.adk.telemetry.setup import maybe_set_otel_providers

    exporters = get_gcp_exporters(
        enable_cloud_tracing=True,
        enable_cloud_logging=True,
    )
    maybe_set_otel_providers([exporters])
