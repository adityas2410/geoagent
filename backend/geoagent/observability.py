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
        # ADK DEBUG output includes complete prompts and model responses. Keep
        # framework logs at INFO or above so ephemeral Q&A content cannot be
        # copied into local console output or Cloud Run logs.
        configured_logger.setLevel(
            max(level, logging.INFO) if logger_name == "google_adk" else level
        )
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
    # Operational telemetry may contain timing and token metadata, but never
    # persist user prompts, chat transcripts, model answers, or hidden thoughts.
    os.environ["OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT"] = "NO_CONTENT"
    os.environ["OTEL_INSTRUMENTATION_GENAI_EMIT_EVENT"] = "false"
    os.environ["ADK_CAPTURE_MESSAGE_CONTENT_IN_SPANS"] = "false"

    from google.adk.telemetry.google_cloud import get_gcp_exporters
    from google.adk.telemetry.google_cloud import get_gcp_resource
    from google.adk.telemetry.setup import maybe_set_otel_providers

    project_id = os.getenv("GOOGLE_CLOUD_PROJECT", "").strip()
    if not project_id:
        raise RuntimeError(
            "GOOGLE_CLOUD_PROJECT is required when GEOAGENT_OTEL_TO_CLOUD is enabled"
        )
    exporters = get_gcp_exporters(
        enable_cloud_tracing=True,
        enable_cloud_logging=True,
    )
    # ADK's GCP trace endpoint requires gcp.project_id on the Resource. The
    # default provider resource contains only generic OTel attributes locally.
    maybe_set_otel_providers(
        [exporters], otel_resource=get_gcp_resource(project_id=project_id)
    )
